"""DecisionStore mixin: entity dictionary and graph."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

try:
    from .store_common import (
        RE_CJK_CHAR, _RE_LATIN_TOK, _entity_key, _valid_entity, extract_entities, normalize
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        RE_CJK_CHAR, _RE_LATIN_TOK, _entity_key, _valid_entity, extract_entities, normalize
    )


class EntityMixin:
    # -- entity dictionary -------------------------------------------------------------------------------------

    def _entities(self) -> dict:
        """In-memory lookup: latin key -> id, cjk key -> id, alias key -> id. Rebuilt lazily after changes."""
        if self._ent_cache is None:
            latin, cjk, alias = {}, {}, {}
            for r in self.conn.execute("SELECT id, key, aliases FROM entities"):
                (cjk if RE_CJK_CHAR.search(r["key"]) else latin)[r["key"]] = r["id"]
                for a in filter(None, r["aliases"].split("|")):
                    k = _entity_key(a)
                    if k and k not in alias:
                        alias[k] = r["id"]
            self._ent_cache = {"latin": latin, "cjk": cjk, "alias": alias,
                               "max_words": max((len(k.split()) for k in (*latin, *alias)), default=1)}
        return self._ent_cache

    def _seed_aliases(self, key: str, name: str) -> str:
        """Aliases from synonyms.yaml groups containing the entity name (e.g. VPS -> 伺服器|主機|server)."""
        return "|".join(m for m in self.synonyms.group_of(name) if _entity_key(m) != key)

    def _get_or_create_entity(self, key: str, name: str, aliases: Iterable[str] = (), seed: bool = True) -> int:
        row = self.conn.execute("SELECT id, aliases FROM entities WHERE key=?", (key,)).fetchone()
        extra = [a for a in aliases if a and _entity_key(a) != key]
        if row:
            if extra:
                merged = "|".join(dict.fromkeys([a for a in row["aliases"].split("|") if a] + extra))
                if merged != row["aliases"]:
                    self.conn.execute("UPDATE entities SET aliases=? WHERE id=?", (merged, row["id"]))
                    self._cache_entity(row["id"], key, extra)
            return row["id"]
        seeded = self._seed_aliases(key, name) if seed else ""
        al = "|".join(dict.fromkeys([a for a in seeded.split("|") if a] + extra))
        eid = self.conn.execute("INSERT INTO entities(key, name, aliases) VALUES (?,?,?)", (key, name, al)).lastrowid
        self._cache_entity(eid, key, al.split("|"))
        return eid

    def _cache_entity(self, eid: int, key: str, aliases: Iterable[str]) -> None:
        c = self._ent_cache
        if c is None:
            return
        (c["cjk"] if RE_CJK_CHAR.search(key) else c["latin"])[key] = eid
        c["max_words"] = max(c["max_words"], len(key.split()))
        for a in aliases:
            k = _entity_key(a) if a else ""
            if k and k not in c["alias"]:
                c["alias"][k] = eid
                c["max_words"] = max(c["max_words"], len(k.split()))

    def detect_entities(self, text: str, aliases: bool = True) -> dict[int, float]:
        """Known entities mentioned in *text* (dictionary lookup over Latin word n-grams and CJK substrings, plus
        aliases). Returns entity id -> match weight (1.0 direct, 0.7 via alias)."""
        cache = self._entities()
        t = normalize(text)
        found: dict[int, float] = {}
        toks = _RE_LATIN_TOK.findall(t)
        n_max = min(cache["max_words"], 4)
        for i in range(len(toks)):
            for n in range(1, n_max + 1):
                if i + n > len(toks):
                    break
                k = " ".join(toks[i:i + n])
                if k in cache["latin"]:
                    found[cache["latin"][k]] = 1.0
                elif aliases and k in cache["alias"]:
                    found.setdefault(cache["alias"][k], 0.7)
        flat = re.sub(r"\s+", "", t)
        if RE_CJK_CHAR.search(flat):
            for k, eid in cache["cjk"].items():
                if k in flat:
                    found[eid] = 1.0
            if aliases:
                for k, eid in cache["alias"].items():
                    if RE_CJK_CHAR.search(k) and k in flat:
                        found.setdefault(eid, 0.7)
        return found

    def _link_entities(self, nid: int, content: str, given: Iterable[str] = (), *, use_dictionary: bool = True,
                       exact: bool = False) -> None:
        """Link the note to its entities: the *given* ones, those found in the text and those the dictionary knows.
        exact=True links the given ones only, accepts any name that has a key (an extracted entity such as a port number
        does not pass the check that an entity named by an agent must), and does not seed aliases from the synonym table
        (an import of Hermind's own export, whose notes already carry the entities they had and the aliases those had)."""
        ents: dict[int, None] = {}
        for raw in given:
            # exact: the source's own entity names, which may contain "=" (aliases are set by the caller)
            name, _, al = (str(raw), "", "") if exact else str(raw).partition("=")
            key = _entity_key(name)
            if key if exact else _valid_entity(key):          # exact: the source's own names, whatever kind of name they are
                ents[self._get_or_create_entity(key, name.strip(), [a.strip() for a in al.split("|")], seed=not exact)] = None
        for key, name in ({} if exact else extract_entities(content)).items():
            # synonym-table aliases only for name-like pattern entities (VPS, llama.cpp), not Title-case words
            seed = bool(RE_CJK_CHAR.search(name) or name.isupper() or re.search(r"[\d._\-+]", name))
            ents[self._get_or_create_entity(key, name, seed=seed)] = None
        if use_dictionary and not exact:
            for eid in self.detect_entities(content, aliases=False):
                ents[eid] = None
        ids = list(ents)[:16]
        if not ids:
            return
        new = [e for e in ids if self.conn.execute(
            "INSERT OR IGNORE INTO note_entities(note_id, entity_id) VALUES (?,?)", (nid, e)).rowcount]
        if new:
            self.conn.executemany("UPDATE entities SET df=df+1 WHERE id=?", [(e,) for e in new])
        ids = sorted(ids)
        pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if a in new or b in new]
        self.conn.executemany("INSERT INTO entity_links(a, b, weight) VALUES (?,?,1) "
                              "ON CONFLICT(a, b) DO UPDATE SET weight=weight+1", pairs)

    def rebuild_graph(self) -> dict:
        """Re-link every note against the final entity dictionary (entities seen late get linked to early notes)."""
        with self._lock, self.conn:
            given = defaultdict(list)  # keep existing links (agent/import supplied entities can't be re-derived)
            for r in self.conn.execute("SELECT e.name, ne.note_id FROM entities e JOIN note_entities ne "
                                       "ON ne.entity_id=e.id"):
                given[r[1]].append(r[0])
            self.conn.execute("DELETE FROM note_entities")
            self.conn.execute("DELETE FROM entity_links")
            self.conn.execute("UPDATE entities SET df=0")
            rows = self.conn.execute("SELECT id, content FROM notes").fetchall()
            for r in rows:
                self._link_entities(r["id"], r["content"], given.get(r["id"], ()))
            self.conn.execute("DELETE FROM entities WHERE df=0 AND aliases='' AND id NOT IN "
                              "(SELECT entity_id FROM note_entities)")
            self._ent_cache = None
            n_ent = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            n_links = self.conn.execute("SELECT COUNT(*) FROM note_entities").fetchone()[0]
        return {"entities": n_ent, "note_entities": n_links}

    def add_entity_aliases(self, name: str, aliases: Iterable[str]) -> int:
        with self._lock, self.conn:
            key = _entity_key(name)
            if not _valid_entity(key):
                raise ValueError(f"not a valid entity name: {name!r}")
            return self._get_or_create_entity(key, name, list(aliases))
