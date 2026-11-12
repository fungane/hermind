"""DecisionStore mixin: add/update/forget, generated + learned keywords, dedup and write checks."""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
from typing import Iterable

try:
    from .store_common import (
        DAY, FIELD_CONTENT, FIELD_GEN, FIELD_KEYWORDS, FIELD_LEARNED, KINDS, LEARNED_MAX_TERMS, RE_CJK_CHAR,
        RefusedWrite, SOURCE_ARMS, _RE_NEGATION, parse_valid, _clean_scope, _clean_tags, _day, _digest, _entity_key,
        _now, _set_sim, classify_kind, core_text, default_expiry, extract_entities, jaccard, l0,
        looks_secret, looks_secret_label, numbers, parse_dates, sim_features, strip_private, has_text_outside_private, token_weight, tokenize
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        DAY, FIELD_CONTENT, FIELD_GEN, FIELD_KEYWORDS, FIELD_LEARNED, KINDS, LEARNED_MAX_TERMS, RE_CJK_CHAR,
        RefusedWrite, SOURCE_ARMS, _RE_NEGATION, parse_valid, _clean_scope, _clean_tags, _day, _digest, _entity_key,
        _now, _set_sim, classify_kind, core_text, default_expiry, extract_entities, jaccard, l0,
        looks_secret, looks_secret_label, numbers, parse_dates, sim_features, strip_private, has_text_outside_private, token_weight, tokenize
    )


log = logging.getLogger(__name__)

_UNSET = object()          # "argument not given" for set_fields (None means: clear the value)
MAX_NOTE_CHARS = 100_000   # one note, in characters; a 10 MB write used to take ~10 s and bloat the DB

FORGOTTEN_REFUSAL = "refused: this content was forgotten earlier (memory_forget)"


def _labels(tags, entities) -> list[str]:
    """Every tag, entity name and entity alias as its own string. An entity is a name, "name=alias|alias" (the documented
    tool syntax) or a (name, aliases) pair; the name and each alias are split off before anything is looked at."""
    out: list[str] = []
    if isinstance(tags, str):
        out += [tags, *tags.replace(",", " ").split()]
    else:
        out += [str(t) for t in (tags or ())]

    def walk(x):
        if isinstance(x, (tuple, list)):
            for y in x:
                walk(y)
        else:
            s = str(x)
            out.append(s)
            name, _, al = s.partition("=")
            out.extend(p.strip() for p in [name, *al.split("|")] if p.strip())
    walk(list(entities or ()))
    return out


def screen_fields(tags="", entities=()) -> None:
    """Raises RefusedWrite when a tag, an entity name or an entity alias is itself a credential (screen.looks_secret_label).
    Each one is looked at on its own: the prose patterns of looks_secret are for note text, not for a label."""
    for label in _labels(tags, entities):
        reason = looks_secret_label(label)
        if reason:
            raise RefusedWrite(f"refused: {reason}; store a description without the secret value")


def clean_entities(entities) -> list:
    """The entities that pass screen_fields, for entities the model extracted (not the caller): an offending one is
    dropped (debug log, never its text) and the note is stored."""
    out, dropped = [], 0
    for e in entities or ():
        try:
            screen_fields(entities=[e])
            out.append(e)
        except RefusedWrite:
            dropped += 1
    if dropped:
        log.debug("dropped %d extracted entit%s that looked like a credential", dropped, "y" if dropped == 1 else "ies")
    return out


def check_text(content: str, keywords: str | Iterable[str] = "", *, screen: bool = True, tags="",
               entities=()) -> tuple[str, str | Iterable[str]]:
    """The store's own rules for a note's text, without touching a database: removes <private> text and returns
    (content, keywords). *tags* and *entities* (names) are screened like the text. Raises ValueError("content is empty") for no text and RefusedWrite for nothing left outside
    <private>, a note over MAX_NOTE_CHARS, or credential-like text. add() and the import core both call this, so a
    preview can ask the question without writing. (The "forgotten earlier" rule needs the tombstones table:
    FORGOTTEN_REFUSAL, is_forgotten().)"""
    content, hidden = strip_private((content or "").strip())
    if isinstance(keywords, str):
        keywords, _ = strip_private(keywords)
    content = content.strip()
    if not content:
        raise ValueError("content is empty")
    if hidden and not has_text_outside_private(content):
        raise RefusedWrite("refused: nothing is left after removing the <private> text")
    if len(content) > MAX_NOTE_CHARS:
        raise RefusedWrite(f"refused: note is {len(content)} characters (limit {MAX_NOTE_CHARS}); store a summary")
    if screen:
        reason = looks_secret(content) or looks_secret(keywords if isinstance(keywords, str) else "")
        if reason:
            raise RefusedWrite(f"refused: {reason}; store a description without the secret value")
        screen_fields(tags, entities)
    return content, keywords


class WriteMixin:
    # -- writes -------------------------------------------------------------------------------------------------

    def add(self, content: str, kind: str = "chat", keywords: str | Iterable[str] = "", *,
            created_at: float | None = None, expires_at: float | None = None, source_session: str = "",
            pinned: bool = False, dedup_threshold: float | None = None, entities: Iterable[str] = (),
            proof_count: int = 1, screen: bool = True, candidate: bool = False, scope: str | None = None,
            tags: str | Iterable[str] = "", review: str = "active", valid_from=None, valid_to=None,
            never_expires: bool = False, touch_existing: bool = True, commit: bool = True,
            exact_entities: bool = False, reset_expiry: bool = True) -> tuple[int, bool]:
        """Insert a note. Returns (id, created). An exact duplicate (or, with *dedup_threshold*, a near-duplicate of
        the same kind) returns the existing id with created=False. kind="auto" classifies from the text.
        Raises RefusedWrite for credential-like content or content that was forgotten (tombstoned).
        review="pending" puts the note in the review queue (searchable but down-weighted until confirmed).
        valid_from / valid_to: when the fact is true in the world (ISO date); both optional, as_of searches use them.
        Keyword-only options for the import core (defaults keep the old behaviour): never_expires=True stores the note
        without an expiry whatever its kind (and, for an exact duplicate that is touched, removes the earlier copy's
        expiry: an explicit save from the CLI or a tool uses it when no kind was chosen, see store_common.explicit_never_expires;
        the same happens when the kind itself never expires, e.g. "decision"; reset_expiry=False turns this off for a writer
        that is not trusted, whose re-save must not change an existing note's expiry); touch_existing=False leaves an exact duplicate untouched (no proof_count
        bump); commit=False leaves the transaction open so the caller can commit many notes at once; exact_entities=True
        links the note to the *entities* given and to nothing else (see _link_entities)."""
        review = "pending" if review == "pending" else "active"
        valid_from, valid_to = parse_valid(valid_from), parse_valid(valid_to)
        if valid_from is not None and valid_to is not None and valid_to <= valid_from:
            raise ValueError("valid_to must be after valid_from")
        tags, entities = (tags if isinstance(tags, str) else list(tags or ())), list(entities or ())
        content, keywords = check_text(content, keywords, screen=screen, tags=tags, entities=entities)
        kind = (kind or "chat").strip().lower()
        if kind == "auto":
            kind = classify_kind(content)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(keywords, str):
            keywords = " ".join(k.strip() for k in keywords if k and k.strip())
        keywords = " ".join(keywords.split())
        now = _now()
        created_at = now if created_at is None else float(created_at)
        future_date = None
        if created_at > now + DAY:
            # A future "created" time is an event date the source mislabelled (for example an imported `date` of "expected January 2028"):
            # keep it as the event time and stamp the note now, or it ranks as the newest note for years.
            future_date, created_at = created_at, now
        if expires_at is None and not pinned and not never_expires:
            expires_at = default_expiry(kind, created_at)
        h = _digest(content)
        with self._lock, (self.conn if commit else contextlib.nullcontext()):
            if self.conn.execute("SELECT 1 FROM tombstones WHERE digest=?", (h,)).fetchone():
                raise RefusedWrite(FORGOTTEN_REFUSAL)
            scope = _clean_scope(scope) if scope else self.scope
            tags = _clean_tags(tags)
            row = self.conn.execute("SELECT id FROM notes WHERE content_hash=? AND scope=?", (h, scope)).fetchone()
            if row:
                if touch_existing:
                    self.conn.execute("UPDATE notes SET proof_count=proof_count+? WHERE id=?",
                                      (max(1, int(proof_count)), row["id"]))
                    # saving the same text again with a write that would itself get no expiry (never_expires, or a kind
                    # without a time to live) keeps it: the earlier copy loses its expiry (not for an untrusted writer)
                    if reset_expiry and expires_at is None and (never_expires or default_expiry(kind, created_at) is None):
                        self.conn.execute("UPDATE notes SET expires_at=NULL WHERE id=?", (row["id"],))
                return row["id"], False
            if dedup_threshold is not None:
                recent = [tuple(r) for r in self.conn.execute(
                    "SELECT id, content FROM notes WHERE kind=? AND scope=? ORDER BY id DESC LIMIT 200", (kind, scope))]
                seen = {r[0] for r in recent}
                # plus the interleaved head of each retrieval arm, so an older near-identical twin is seen too
                twins = [(i, self._content(i)) for i in self.dedup_candidates(content, kind=kind) if i not in seen]
                for rid, rc in recent + twins:
                    if rc and jaccard(rc, content) >= dedup_threshold:
                        return rid, False
            ctoks, ktoks = tokenize(content), tokenize(keywords)
            ev = parse_dates(core_text(content), ref=created_at) or parse_dates(content, ref=created_at)
            cur = self.conn.execute(
                "INSERT INTO notes(content, kind, keywords, created_at, expires_at, source_session, pinned, "
                "content_hash, doc_len_content, doc_len_keywords, event_at, proof_count, candidate, scope, tags, review, "
                "valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (content, kind, keywords, created_at, expires_at, source_session or "", int(bool(pinned)), h,
                 len(ctoks), len(ktoks), ev[0] if ev else future_date, max(1, int(proof_count)), int(bool(candidate)),
                 scope, tags, review, valid_from, valid_to))
            nid = cur.lastrowid
            self._index(nid, ctoks, ktoks)
            self._link_entities(nid, content, entities, exact=exact_entities)
            return nid, True

    def _index(self, nid: int, ctoks: list[str], ktoks: list[str]) -> None:
        rows = [(t, nid, FIELD_CONTENT, n) for t, n in Counter(ctoks).items()]
        rows += [(t, nid, FIELD_KEYWORDS, n) for t, n in Counter(ktoks).items()]
        self.conn.executemany("INSERT OR REPLACE INTO postings(term, note_id, field, tf) VALUES (?,?,?,?)", rows)

    def update_keywords(self, note_id: int, keywords: str) -> bool:
        keywords = " ".join((keywords or "").split())
        with self._lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
                return False
            ktoks = tokenize(keywords)
            self.conn.execute("UPDATE notes SET keywords=?, doc_len_keywords=? WHERE id=?",
                              (keywords, len(ktoks), note_id))
            self.conn.execute("DELETE FROM postings WHERE note_id=? AND field=?", (note_id, FIELD_KEYWORDS))
            self.conn.executemany("INSERT INTO postings(term, note_id, field, tf) VALUES (?,?,?,?)",
                                  [(t, note_id, FIELD_KEYWORDS, n) for t, n in Counter(ktoks).items()])
            return True

    def _index_field(self, nid: int, field: int, text: str) -> None:
        self.conn.execute("DELETE FROM postings WHERE note_id=? AND field=?", (nid, field))
        self.conn.executemany("INSERT INTO postings(term, note_id, field, tf) VALUES (?,?,?,?)",
                              [(t, nid, field, n) for t, n in Counter(tokenize(text)).items()])

    # -- v0.4: generated aliases (offline backfill) ---------------------------------------------------------------

    def set_gen_keywords(self, note_id: int, aliases: Iterable[str], model: str = "", now: float | None = None) -> bool:
        """Store LLM-generated alternative phrasings in gen_keywords (never touches the agent-written keywords) and
        index them as their own posting field. Aliases are '|'-separated in the column."""
        text = " | ".join(a for a in aliases if a)
        now = _now() if now is None else now
        with self._lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
                return False
            self.conn.execute("UPDATE notes SET gen_keywords=?, gen_at=?, gen_model=? WHERE id=?",
                              (text, now, str(model or "")[:80], note_id))
            self._index_field(note_id, FIELD_GEN, text)
            return True

    def clear_gen_keywords(self, kinds: Iterable[str] = ()) -> int:
        kinds = [k for k in kinds if k]
        with self._lock, self.conn:
            sql = "SELECT id FROM notes WHERE gen_at IS NOT NULL"
            if kinds:
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            ids = [r[0] for r in self.conn.execute(sql, kinds)]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                self.conn.execute(f"UPDATE notes SET gen_keywords='', gen_at=NULL, gen_model='' WHERE id IN ({q})",
                                  chunk)
                self.conn.execute(f"DELETE FROM postings WHERE field={FIELD_GEN} AND note_id IN ({q})", chunk)
            return len(ids)

    def backfill_targets(self, kinds: Iterable[str] = (), since: float | None = None, max_keywords: int = 4,
                         now: float | None = None, after_id: int = 0, batch: int = 200) -> tuple[list[dict], int | None]:
        """One page of backfill work: active, unexpired notes (id > after_id, at most *batch* scanned) without
        generated aliases whose agent keywords are empty or short (<= max_keywords words), and the last scanned id
        (None when nothing is left). Paged so the caller never holds the whole table."""
        now = _now() if now is None else now
        sql = ("SELECT id, content, keywords, kind FROM notes WHERE gen_at IS NULL AND status='active' AND id > ? "
               "AND (expires_at IS NULL OR expires_at > ? OR pinned=1)")
        args: list = [after_id, now]
        kinds = [k for k in kinds if k]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(since)
        sql += " ORDER BY id LIMIT ?"
        args.append(int(batch))
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return ([{"id": r["id"], "content": r["content"], "keywords": r["keywords"], "kind": r["kind"]} for r in rows
                 if len(r["keywords"].split()) <= max_keywords], rows[-1]["id"] if rows else None)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))

    # -- v0.4: learning from use ------------------------------------------------------------------------------------

    def learnable_terms(self, note_id: int, query: str) -> list[str]:
        """Query terms worth learning for a note: weighted tokens (no CJK single characters, no particles) that the
        note's content / keywords / generated aliases / learned terms do not already contain."""
        with self._lock:
            have = {r[0] for r in self.conn.execute("SELECT term FROM postings WHERE note_id=?", (note_id,))}
        out = []
        for tok in dict.fromkeys(tokenize(query)):
            if token_weight(tok) <= 0 or tok in have or (len(tok) == 1 and RE_CJK_CHAR.match(tok)):
                continue
            if tok.isdigit() or len(tok) > 40:
                continue
            out.append(tok)
        return out[:LEARNED_MAX_TERMS]

    def learn(self, note_id: int, query: str, source: str, now: float | None = None) -> list[str]:
        """Record one confirmation that *query* led to *note_id* (source = session id + day). A term is indexed in
        the note's learned_terms only after >= 2 independent confirmations (different sessions or days); until then
        it waits in learn_pending. Returns the terms promoted by this call."""
        now = _now() if now is None else now
        terms = self.learnable_terms(note_id, query)
        if not terms:
            return []
        qd = _digest(query)
        promoted = []
        with self._lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
                return []
            self.conn.executemany("INSERT OR IGNORE INTO learn_pending(note_id, term, source, query_digest, created_at) "
                                  "VALUES (?,?,?,?,?)", [(note_id, t, source, qd, now) for t in terms])
            for t in terms:
                n = self.conn.execute("SELECT COUNT(*) FROM learn_pending WHERE note_id=? AND term=?",
                                      (note_id, t)).fetchone()[0]
                if n >= 2:
                    promoted.append(t)
            if promoted:
                cur = self.conn.execute("SELECT learned_terms FROM notes WHERE id=?", (note_id,)).fetchone()[0].split()
                cur = [t for t in cur if t not in promoted] + promoted  # oldest first; re-confirmed move to the end
                self._set_learned(note_id, cur[-LEARNED_MAX_TERMS:])
                self.conn.execute(f"DELETE FROM learn_pending WHERE note_id=? AND term IN "
                                  f"({','.join('?' * len(promoted))})", (note_id, *promoted))
        return promoted

    def unlearn(self, note_id: int, query: str) -> list[str]:
        """Negative feedback: drop the terms of *query* from the note's learned terms and pending confirmations."""
        toks = set(tokenize(query))
        with self._lock, self.conn:
            r = self.conn.execute("SELECT learned_terms FROM notes WHERE id=?", (note_id,)).fetchone()
            if not r:
                return []
            cur = r[0].split()
            removed = [t for t in cur if t in toks]
            if removed:
                self._set_learned(note_id, [t for t in cur if t not in toks])
            self.conn.executemany("DELETE FROM learn_pending WHERE note_id=? AND term=?",
                                  [(note_id, t) for t in toks])
            self.conn.execute("DELETE FROM learn_pending WHERE note_id=? AND query_digest=?", (note_id, _digest(query)))
        return removed

    def _set_learned(self, note_id: int, terms: list[str]) -> None:
        text = " ".join(terms)
        self.conn.execute("UPDATE notes SET learned_terms=? WHERE id=?", (text, note_id))
        self.conn.execute("DELETE FROM postings WHERE note_id=? AND field=?", (note_id, FIELD_LEARNED))
        self.conn.executemany("INSERT INTO postings(term, note_id, field, tf) VALUES (?,?,?,1)",
                              [(t, note_id, FIELD_LEARNED) for t in dict.fromkeys(terms)])

    def _delete_ids(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            # one off for every deleted note that is linked to the entity: notes that share an entity lower it together
            self.conn.executemany("UPDATE entities SET df=df-? WHERE id=?", self.conn.execute(
                f"SELECT COUNT(*), entity_id FROM note_entities WHERE note_id IN ({q}) GROUP BY entity_id", chunk).fetchall())
            for sql in (f"DELETE FROM postings WHERE note_id IN ({q})",
                        f"DELETE FROM learn_pending WHERE note_id IN ({q})",
                        f"DELETE FROM vectors WHERE note_id IN ({q})",
                        f"DELETE FROM note_entities WHERE note_id IN ({q})",
                        f"DELETE FROM notes WHERE id IN ({q})"):
                self.conn.execute(sql, chunk)
            # Notes merged into / superseded by a deleted note come back as active rows.
            self.conn.execute(f"UPDATE notes SET status='active', canonical_id=NULL WHERE canonical_id IN ({q})",
                              chunk)
            self.conn.execute(f"DELETE FROM note_links WHERE src IN ({q}) OR dst IN ({q})", chunk + chunk)

    def forget(self, note_id: int, now: float | None = None) -> bool:
        """Delete a note together with its merged duplicates and its own edit history, and leave a tombstone
        (normalised digest) for each, so auto-capture cannot re-create them. Older states superseded by it come
        back as active notes."""
        now = _now() if now is None else now
        with self._lock, self.conn:
            row = self.conn.execute("SELECT content_hash FROM notes WHERE id=?", (note_id,)).fetchone()
            if not row:
                return False
            behind = [r[0] for r in self.conn.execute(
                "SELECT src FROM note_links WHERE dst=? AND rel IN ('merged_into', 'version_of')", (note_id,))]
            ids = [note_id] + behind
            q = ",".join("?" * len(ids))
            self.conn.executemany("INSERT OR IGNORE INTO tombstones(digest, note_id, created_at) VALUES (?,?,?)",
                                  [(_digest(r[0]), r[1], now) for r in self.conn.execute(
                                      f"SELECT content, id FROM notes WHERE id IN ({q})", ids)])
            self._delete_ids(ids)
            return True

    def is_forgotten(self, content: str) -> bool:
        h = _digest(content)
        with self._lock:
            return self.conn.execute("SELECT 1 FROM tombstones WHERE digest=?", (h,)).fetchone() is not None

    def feedback(self, note_id: int, helpful: bool) -> float | None:
        """Trust feedback: +0.1 when helpful, -0.2 when not (clamped 0..1). Returns the new trust, None if no note."""
        with self._lock, self.conn:
            cur = self.conn.execute("UPDATE notes SET trust=MIN(1.0, MAX(0.0, trust + ?)) WHERE id=?",
                                    (0.1 if helpful else -0.2, note_id))
            if not cur.rowcount:
                return None
            return round(self.conn.execute("SELECT trust FROM notes WHERE id=?", (note_id,)).fetchone()[0], 3)

    def update(self, note_id: int, content: str, keywords: str | None = None, now: float | None = None) -> bool:
        """Replace a note's text in place (same id). Raises RefusedWrite for credential-like text and for text that was
        forgotten (tombstoned), like add(). The old text is kept as a hidden history row
        (status='superseded', valid_to=now, link 'version_of') so reflect/history still show what it said. The note
        itself gets valid_from=now if it had none (the new text is valid from the update), so an as_of search before
        the update finds the old version and one after it finds the new text."""
        now = _now() if now is None else now
        content, hidden = strip_private((content or "").strip())
        keywords = strip_private(keywords)[0] if keywords else keywords
        content = content.strip()
        if not content:
            raise ValueError("content is empty")
        if hidden and not has_text_outside_private(content):
            raise RefusedWrite("refused: nothing is left after removing the <private> text")
        reason = looks_secret(content) or looks_secret(keywords or "")
        if reason:
            raise RefusedWrite(f"refused: {reason}; store a description without the secret value")
        with self._lock, self.conn:
            if self.conn.execute("SELECT 1 FROM tombstones WHERE digest=?", (_digest(content),)).fetchone():
                raise RefusedWrite(FORGOTTEN_REFUSAL)       # the same rule as add(): forgotten text does not come back
            self.conn.execute("DELETE FROM vectors WHERE note_id=?", (note_id,))   # text changed: re-embed later
            old = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if not old:
                return False
            if keywords is None:
                keywords = old["keywords"]
            keywords = " ".join(keywords.split())
            h = _digest(content)
            if h == old["content_hash"] and keywords == old["keywords"]:
                return True
            hist = self._insert_history_row(
                content=old["content"], kind=old["kind"], keywords=old["keywords"], created_at=old["created_at"],
                expires_at=old["expires_at"], source_session=old["source_session"], pinned=old["pinned"],
                doc_len_content=old["doc_len_content"], doc_len_keywords=old["doc_len_keywords"],
                event_at=old["event_at"], proof_count=1, status="superseded", canonical_id=note_id, consolidated_at=now,
                trust=old["trust"], valid_to=now if old["valid_to"] is None else min(now, old["valid_to"]),
                scope=old["scope"], tags=old["tags"], review=old["review"], valid_from=old["valid_from"])
            self.conn.execute("UPDATE postings SET note_id=? WHERE note_id=?", (hist, note_id))
            self.conn.execute("UPDATE note_entities SET note_id=? WHERE note_id=?", (hist, note_id))
            self.conn.execute("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,?,?)",
                              (hist, note_id, "version_of", now))
            ctoks, ktoks = tokenize(content), tokenize(keywords)
            ev = parse_dates(core_text(content), ref=now) or parse_dates(content, ref=now)
            self.conn.execute("UPDATE notes SET content=?, keywords=?, content_hash=?, doc_len_content=?, "
                              "doc_len_keywords=?, event_at=?, consolidated_at=NULL, candidate=0, "
                              "valid_from=COALESCE(valid_from, ?) WHERE id=?",
                              (content, keywords, h, len(ctoks), len(ktoks), ev[0] if ev else old["event_at"],
                               now, note_id))
            self._index(note_id, ctoks, ktoks)
            self.conn.execute("UPDATE notes SET gen_keywords='', gen_at=NULL, gen_model='' WHERE id=?", (note_id,))
            self.conn.execute("UPDATE postings SET note_id=? WHERE note_id=? AND field=?", (note_id, hist, FIELD_LEARNED))
            self.conn.execute("DELETE FROM postings WHERE note_id=? AND field=?", (hist, FIELD_GEN))
            given = [r[0] for r in self.conn.execute(
                "SELECT e.name FROM note_entities ne JOIN entities e ON e.id=ne.entity_id WHERE ne.note_id=?",
                (hist,))]
            self._link_entities(note_id, content, given)
            return True

    def _insert_history_row(self, *, content, kind, keywords, created_at, expires_at, source_session, pinned,
                            doc_len_content, doc_len_keywords, event_at, proof_count, status, canonical_id,
                            consolidated_at, trust, valid_to, scope, tags, review, valid_from) -> int:
        """Insert the hidden row of an older version of a note (status superseded / merged, canonical_id = the note it
        belongs to, locked so consolidation leaves it alone) and return its id. update() and add_older_version() both
        write history rows through here. The caller indexes it and links it."""
        return self.conn.execute(
            "INSERT INTO notes(content, kind, keywords, created_at, expires_at, source_session, pinned, "
            "content_hash, doc_len_content, doc_len_keywords, event_at, proof_count, status, canonical_id, "
            "consolidated_at, locked, trust, valid_to, scope, tags, review, valid_from) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (content, kind, keywords, created_at, expires_at, source_session, pinned, "", doc_len_content,
             doc_len_keywords, event_at, proof_count, status, canonical_id, consolidated_at, trust, valid_to, scope,
             tags, review, valid_from)).lastrowid

    def add_older_version(self, note_id: int, content: str, *, kind: str, keywords: str = "", created_at: float,
                          status: str = "superseded", rel: str = "version_of", valid_from: float | None = None,
                          valid_to: float | None = None, event_at: float | None = None, proof_count: int = 1,
                          trust: float = 0.5, scope: str | None = None, tags: str | Iterable[str] = "",
                          review: str = "active", pinned: bool = False, source_session: str = "",
                          entities: Iterable[str] = (), now: float | None = None, commit: bool = True,
                          exact_entities: bool = False) -> int:
        """Store *content* as an older version of note *note_id*, the way update() keeps the text it replaces: a hidden
        row (status superseded or merged, canonical_id = note_id), a link *rel* (version_of, superseded_by or
        merged_into) from the old row to the note, and a search index of its own, so an include_history or as_of search
        finds it. The text goes through no rules here (the caller applied them). Never expires. Returns the row's id."""
        if status not in ("superseded", "merged"):
            raise ValueError("status must be superseded or merged")
        now = _now() if now is None else now
        keywords = " ".join((keywords or "").split())
        ctoks, ktoks = tokenize(content), tokenize(keywords)
        with self._lock, (self.conn if commit else contextlib.nullcontext()):
            hist = self._insert_history_row(
                content=content, kind=kind, keywords=keywords, created_at=created_at, expires_at=None,
                source_session=source_session or "", pinned=int(bool(pinned)), doc_len_content=len(ctoks),
                doc_len_keywords=len(ktoks), event_at=event_at, proof_count=max(1, int(proof_count)), status=status,
                canonical_id=note_id, consolidated_at=now, trust=trust, valid_to=valid_to,
                scope=_clean_scope(scope) if scope else self.scope, tags=_clean_tags(tags), review=review,
                valid_from=valid_from)
            self._index(hist, ctoks, ktoks)
            self.conn.execute("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,?,?)",
                              (hist, note_id, rel, now))
            self._link_entities(hist, content, entities, exact=exact_entities)
            return hist

    def restore_fields(self, note_id: int, *, event_at=_UNSET, proof_count=_UNSET, gen_keywords=_UNSET, gen_at=_UNSET,
                       gen_model=_UNSET, learned_terms=_UNSET, commit: bool = True) -> None:
        """Set what a note learned elsewhere, exactly as given (an import of Hermind's own export carries it over):
        event date, proof count, generated aliases (indexed as their own field, like set_gen_keywords) and learned terms
        (indexed like _set_learned). Arguments left out are untouched; the counters of use are never set here."""
        with self._lock, (self.conn if commit else contextlib.nullcontext()):
            sets, args = [], []
            if event_at is not _UNSET:
                sets.append("event_at=?"); args.append(None if event_at is None else float(event_at))
            if proof_count is not _UNSET:
                sets.append("proof_count=?"); args.append(max(1, int(proof_count)))
            if gen_keywords is not _UNSET:
                sets.append("gen_keywords=?"); args.append(str(gen_keywords or ""))
            if gen_at is not _UNSET:
                sets.append("gen_at=?"); args.append(None if gen_at is None else float(gen_at))
            if gen_model is not _UNSET:
                sets.append("gen_model=?"); args.append(str(gen_model or "")[:80])
            if sets:
                self.conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id=?", (*args, note_id))
            if gen_keywords is not _UNSET:
                self._index_field(note_id, FIELD_GEN, str(gen_keywords or ""))
            if learned_terms is not _UNSET:
                self._set_learned(note_id, str(learned_terms or "").split())

    def set_fields(self, note_id: int, *, tags=_UNSET, trust=_UNSET, kind=_UNSET, valid_from=_UNSET,
                   valid_to=_UNSET) -> bool:
        """Edit metadata that is not part of the text (no history row): tags, trust (0..1), kind, valid_from, valid_to
        (None clears a date). Arguments left out are untouched. Raises ValueError for a bad kind or date range.
        Returns False if there is no such note."""
        sets, args = [], []
        if tags is not _UNSET:
            screen_fields(tags)
            sets.append("tags=?"); args.append(_clean_tags(tags))
        if trust is not _UNSET:
            sets.append("trust=?"); args.append(min(1.0, max(0.0, float(trust))))
        if kind is not _UNSET:
            kind = (kind or "").strip().lower()
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            sets += ["kind=?", "consolidated_at=NULL"]; args.append(kind)
        with self._lock, self.conn:
            row = self.conn.execute("SELECT valid_from, valid_to FROM notes WHERE id=?", (note_id,)).fetchone()
            if not row:
                return False
            vf = row["valid_from"] if valid_from is _UNSET else parse_valid(valid_from)
            vt = row["valid_to"] if valid_to is _UNSET else parse_valid(valid_to)
            if vf is not None and vt is not None and vt <= vf:
                raise ValueError("valid_to must be after valid_from")
            if valid_from is not _UNSET:
                sets.append("valid_from=?"); args.append(vf)
            if valid_to is not _UNSET:
                sets.append("valid_to=?"); args.append(vt)
            if sets:
                self.conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id=?", (*args, note_id))
            return True

    def find_by_content(self, content: str, source_session: str | None = None) -> int | None:
        """Id of the note whose normalised text equals *content* (optionally from one source), else None."""
        sql, args = "SELECT id FROM notes WHERE content_hash=?", [_digest(content)]
        if source_session is not None:
            sql += " AND source_session=?"
            args.append(source_session)
        with self._lock:
            r = self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return r[0] if r else None

    def _content(self, note_id: int) -> str:
        r = self.conn.execute("SELECT content FROM notes WHERE id=?", (note_id,)).fetchone()
        return r[0] if r else ""

    def dedup_candidates(self, content: str, kind: str | None = None, per_arm: int = 3) -> list[int]:
        """Twin candidates for dedup: the interleaved heads of the retrieval arms (each arm's #1, #2, ...), so a
        near-identical note is seen even when plain RRF would rank it below notes strong on several arms."""
        try:
            return [h["id"] for h in self.search(core_text(content)[:400], limit=per_arm * len(SOURCE_ARMS),
                                                 kind=kind, fusion="interleave", rare_fallback=False)]
        except Exception:  # never let a dedup probe break a write
            return []

    def write_check(self, content: str, entities: Iterable[str] = (), limit: int = 5) -> dict:
        """Before storing: notes that say nearly the same thing ('similar') and notes about the same entity whose
        numbers or negation differ ('possible_conflicts'), for the calling agent to decide update vs add."""
        cands = self.dedup_candidates(content)
        if not cands:
            return {"similar": [], "possible_conflicts": []}
        with self._lock:
            mine_ents = set(self.detect_entities(content, aliases=False))
            for raw in entities:
                k = _entity_key(str(raw).partition("=")[0])
                r = self.conn.execute("SELECT id FROM entities WHERE key=?", (k,)).fetchone()
                if r:
                    mine_ents.add(r[0])
            mine_ents |= {self._entities()["latin"].get(k) or self._entities()["cjk"].get(k)
                          for k in extract_entities(content)} - {None}
            ents = self.note_entities(cands)
            info = self._fetch(cands)
        f_new, n_new, neg_new = sim_features(content), numbers(content), bool(_RE_NEGATION.search(core_text(content)))
        similar, conflicts = [], []
        for i in cands:
            r = info.get(i)
            if r is None or r["status"] != "active":
                continue
            j, c = _set_sim(f_new, sim_features(r["content"]))
            item = {"id": i, "kind": r["kind"], "date": _day(r["event_at"] or r["created_at"]),
                    "text": l0(r["content"]), "similarity": round(j, 2)}
            shared = mine_ents & {e for e, _, _ in ents.get(i, [])}
            n_old = numbers(r["content"])
            neg_old = bool(_RE_NEGATION.search(core_text(r["content"])))
            if shared and j >= 0.2 and ((n_new and n_old and n_new != n_old) or neg_new != neg_old):
                item["why"] = "different numbers" if (n_new and n_old and n_new != n_old) else "negation differs"
                conflicts.append(item)
            elif j >= 0.45 or c >= 0.8:
                similar.append(item)
        return {"similar": similar[:limit], "possible_conflicts": conflicts[:limit]}
