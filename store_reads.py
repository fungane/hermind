"""DecisionStore mixin: get/list/count/stats and entity lookups."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable

try:
    from .store_common import (
        _clean_tags, _entity_key, _now, day_bounds
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        _clean_tags, _entity_key, _now, day_bounds
    )


class ReadMixin:
    # -- reads --------------------------------------------------------------------------------------------------

    def get(self, note_id: int) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, kind: str | None = None, limit: int = 20, now: float | None = None,
             include_expired: bool = False, include_hidden: bool = False, scope: str | None = None,
             tags: str | Iterable[str] = "") -> list[dict]:
        now = _now() if now is None else now
        sql, args = "SELECT * FROM notes WHERE 1=1", []
        sc = self._scopes(scope)
        if sc is not None:
            sql += f" AND scope IN ({','.join('?' * len(sc))})"
            args += sc
        for t in _clean_tags(tags).split():
            sql += " AND (' ' || tags || ' ') LIKE ?"
            args.append(f"% {t} %")
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > ? OR pinned=1)"
            args.append(now)
        if not include_hidden:
            sql += " AND status='active'"
        sql += " ORDER BY pinned DESC, created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit or 20), 500)))
        with self._lock:
            return [self._row(r) for r in self.conn.execute(sql, args)]

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def stats(self) -> dict:
        with self._lock:
            kinds = dict(self.conn.execute("SELECT kind, COUNT(*) FROM notes WHERE status='active' GROUP BY kind"))
            status = dict(self.conn.execute("SELECT status, COUNT(*) FROM notes GROUP BY status"))
            ents = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            links = self.conn.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0]
        return {"notes": sum(status.values()), "by_status": status, "active_by_kind": kinds, "entities": ents,
                "entity_links": links}

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in ("id", "content", "kind", "keywords", "created_at", "expires_at", "source_session",
                               "pinned")}
        keys = r.keys()
        for k in ("status", "canonical_id", "proof_count", "event_at", "trust", "valid_to", "candidate", "scope", "tags",
                  "access_count", "last_access", "gen_keywords", "gen_at", "gen_model", "learned_terms", "review", "valid_from",
                  "shown_count", "opened_count", "fb_at", "locked"):
            if k in keys:
                d[k] = r[k]
        return d

    def note_entities(self, note_ids: Iterable[int]) -> dict[int, list[tuple[int, str, int]]]:
        ids = list(note_ids)
        out: dict[int, list] = defaultdict(list)
        if not ids:
            return out
        q = ",".join("?" * len(ids))
        for r in self.conn.execute(f"SELECT ne.note_id, e.id, e.name, e.df FROM note_entities ne JOIN entities e "
                                   f"ON e.id=ne.entity_id WHERE ne.note_id IN ({q})", ids):
            out[r[0]].append((r[1], r[2], r[3]))
        return out

    def related(self, entity: str, limit: int = 20, now: float | None = None, timeline: bool = False,
                as_of=None) -> dict:
        """Notes about an entity (name or alias) plus its most co-mentioned entities. timeline=True lists them oldest
        first and keeps superseded versions (merged duplicates stay hidden), each marked current / outdated.
        as_of ('YYYY-MM-DD'): only notes valid on that day (see search); superseded versions are kept."""
        now = _now() if now is None else now
        as_end = day_bounds(as_of)[1] if as_of not in (None, "") else None
        vsql = (" AND COALESCE(n.valid_from, n.created_at) < ? AND (n.valid_to IS NULL OR n.valid_to >= ?)"
                if as_end is not None else "")
        vargs = (as_end, as_end) if as_end is not None else ()
        timeline = timeline or as_end is not None
        with self._lock:
            ids = self.detect_entities(entity)
            key = _entity_key(entity)
            row = self.conn.execute("SELECT id FROM entities WHERE key=?", (key,)).fetchone()
            if row:
                ids = {row["id"]: 1.0, **ids}
            if not ids:
                return {"entity": entity, "found": False, "notes": [], "related_entities": []}
            eid = next(iter(ids))
            ent = self.conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
            statuses = "('active','superseded')" if timeline else "('active')"
            order = "ASC" if timeline else "DESC"
            rows = self.conn.execute(
                "SELECT n.* FROM note_entities ne JOIN notes n ON n.id=ne.note_id WHERE ne.entity_id=? AND "
                f"n.status IN {statuses} AND (n.expires_at IS NULL OR n.expires_at > ? OR n.pinned=1 OR n.status='superseded')"
                f"{vsql} ORDER BY COALESCE(n.event_at, n.created_at) {order}, n.id {order} LIMIT ?",
                (eid, now, *vargs, int(limit))).fetchall()
            if timeline and len(rows) == int(limit):   # keep the NEWEST `limit` notes, still shown oldest first
                rows = self.conn.execute(
                    "SELECT * FROM (SELECT n.* FROM note_entities ne JOIN notes n ON n.id=ne.note_id WHERE ne.entity_id=? "
                    "AND n.status IN ('active','superseded') AND (n.expires_at IS NULL OR n.expires_at > ? OR n.pinned=1 "
                    f"OR n.status='superseded'){vsql} ORDER BY COALESCE(n.event_at, n.created_at) DESC, n.id DESC "
                    "LIMIT ?) ORDER BY COALESCE(event_at, created_at) ASC, id ASC",
                    (eid, now, *vargs, int(limit))).fetchall()
            notes = [self._row(r) for r in rows]
            if timeline:
                stale = self.stale_ids([n["id"] for n in notes])
                for n in notes:
                    if n["id"] in stale:
                        n["outdated_by"] = stale[n["id"]]
            rel = [{"name": r[0], "weight": r[1]} for r in self.conn.execute(
                "SELECT e.name, l.weight FROM entity_links l JOIN entities e ON e.id = CASE WHEN l.a=? THEN l.b "
                "ELSE l.a END WHERE l.a=? OR l.b=? ORDER BY l.weight DESC LIMIT 10", (eid, eid, eid))]
        return {"entity": ent["name"], "found": True, "aliases": [a for a in ent["aliases"].split("|") if a],
                "mentions": ent["df"], "notes": notes, "related_entities": rel}
