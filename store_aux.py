"""DecisionStore mixin: usage log, entity summaries, semantic-vector storage, access tracking."""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Iterable

try:
    from .store_common import (
        ACCESS_FLUSH_SECONDS, DAY, TRUST_HIDE_BELOW, _clean_tags, _now, _parse_when, day_bounds, strip_private, valid_at
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        ACCESS_FLUSH_SECONDS, DAY, TRUST_HIDE_BELOW, _clean_tags, _now, _parse_when, day_bounds, strip_private, valid_at
    )


class AuxMixin:
    # -- usage log (so a user can tell whether recall works in real use; local only, small, prunable) ----------------

    def _ensure_log(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
                source TEXT, query TEXT, n_queries INTEGER, n_results INTEGER, note_id INTEGER, helpful INTEGER);
            -- which notes a search or automatic recall listed (ids and rank only, no text), so that a later open
            -- (kind 'get' or 'feedback', search_id = the listing event) can be matched to the question that led to it
            CREATE TABLE IF NOT EXISTS recall_shown (
                event_id INTEGER NOT NULL, note_id INTEGER NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY (event_id, note_id));
        """)
        if not getattr(self, "_log_migrated", False):      # databases made before this column existed
            if "search_id" not in [r[1] for r in self.conn.execute("PRAGMA table_info(usage_log)")]:
                self.conn.execute("ALTER TABLE usage_log ADD COLUMN search_id INTEGER")
            self._log_migrated = True

    def log_event(self, kind: str, *, source: str = "", query: str = "", n_queries: int = 1,
                  n_results: int = 0, note_id: int | None = None, helpful: bool | None = None,
                  keep_days: int = 90, shown: list | None = None, search_id: int | None = None) -> int | None:
        """Record one search ('search') or feedback ('feedback') event and return its row id (None when logging failed).
        shown = ids the search listed, best first; search_id = for 'get' / 'feedback', the event that listed the note.
        Never raises: logging must not break recall."""
        try:
            with self._lock, self.conn:
                self._ensure_log()
                cur = self.conn.execute(
                    "INSERT INTO usage_log(ts, kind, source, query, n_queries, n_results, note_id, helpful, search_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (_now(), kind, source, strip_private(query or "")[0][:200], n_queries, n_results, note_id,
                     None if helpful is None else int(bool(helpful)), search_id))
                eid = cur.lastrowid
                if shown:
                    self.conn.executemany("INSERT OR IGNORE INTO recall_shown(event_id, note_id, rank) VALUES (?,?,?)",
                                          [(eid, int(i), r) for r, i in enumerate(dict.fromkeys(shown))])
                if self.conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] % 200 == 0:
                    self.conn.execute("DELETE FROM usage_log WHERE ts < ?", (_now() - keep_days * 86400,))
                    self.conn.execute("DELETE FROM recall_shown WHERE event_id NOT IN (SELECT id FROM usage_log)")
                return eid
        except Exception:
            return None

    def usage_report(self, days: int = 30) -> dict:
        with self._lock, self.conn:
            self._ensure_log()
            since = _now() - days * 86400
            q = lambda sql, *a: self.conn.execute(sql, a).fetchall()
            tot = q("SELECT source, COUNT(*), SUM(n_results=0), SUM(n_queries>1) FROM usage_log "
                    "WHERE kind='search' AND ts>=? GROUP BY source", since)
            fb = q("SELECT helpful, COUNT(*) FROM usage_log WHERE kind='feedback' AND ts>=? GROUP BY helpful", since)
            miss = q("SELECT query, ts FROM usage_log WHERE kind='search' AND n_results=0 AND ts>=? "
                     "ORDER BY ts DESC LIMIT 15", since)
            gets = self.conn.execute("SELECT COUNT(*) FROM usage_log WHERE kind='get' AND ts>=?", (since,)).fetchone()[0]
            nonotes = self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            llm = q("SELECT kind, COUNT(*), COALESCE(SUM(n_queries),0), SUM(n_results<0 OR (kind='rerank' AND n_results=0)) "
                    "FROM usage_log WHERE kind IN ('rerank','extract') AND ts>=? GROUP BY kind", since)
        return {"days": days, "notes": nonotes, "gets": gets, "open_rate": self.open_rate_report(),
                "llm": {r[0]: {"calls": r[1], "prompt_tokens": r[2], "no_answer": r[3] or 0} for r in llm},
                "searches": {r[0] or "?": {"count": r[1], "zero_result": r[2] or 0, "with_rewrites": r[3] or 0} for r in tot},
                "feedback": {("helpful" if r[0] else "not_helpful"): r[1] for r in fb},
                "recent_zero_result_queries": [r[0] for r in miss]}

    def replace_summaries(self, entity: str, items: list, now: float | None = None) -> list[int]:
        """Replace the consolidated summaries of one entity. *items* = [(text, [source note ids]), ...]. Summaries are
        kind='summary', locked (never merged/superseded by consolidate()), and cite their sources as '#id'. The source
        notes themselves are untouched. Returns the new ids."""
        now = _now() if now is None else now
        key = "summary:" + entity
        with self._lock, self.conn:
            old = [r[0] for r in self.conn.execute("SELECT id FROM notes WHERE kind='summary' AND source_session=?", (key,))]
            if old:
                self._delete_ids(old)
            ids = []
            for text, srcs in items:
                srcs = sorted({int(i) for i in srcs})
                nid, created = self.add(f"{text.strip()} (綜合 {' '.join('#%d' % i for i in srcs)})", kind="summary",
                                        entities=[entity], source_session=key, proof_count=max(1, len(srcs)),
                                        tags="summary", created_at=now)
                if created:
                    self.conn.execute("UPDATE notes SET locked=1, consolidated_at=? WHERE id=?", (now, nid))
                    ids.append(nid)
        return ids

    def summary_targets(self, min_notes: int = 3, max_notes: int = 40, stale_only: bool = False) -> list[tuple[str, int]]:
        """[(entity, n_notes)] for entities with enough active, non-summary notes to be worth consolidating.
        stale_only: only entities that have no summary yet or got a newer note since their summary was written
        (the incremental 'keep the observations current' pass; the idea is credited to Hindsight)."""
        sql = ("SELECT e.name, COUNT(*) c, MAX(n.created_at) newest FROM entities e JOIN note_entities ne ON ne.entity_id=e.id "
               "JOIN notes n ON n.id=ne.note_id AND n.status='active' AND n.review='active' AND n.kind!='summary' "
               "GROUP BY e.id HAVING c BETWEEN ? AND ? ORDER BY c DESC")
        with self._lock:
            rows = self.conn.execute(sql, (min_notes, max_notes)).fetchall()
            if not stale_only:
                return [(r[0], r[1]) for r in rows]
            made = dict(self.conn.execute(
                "SELECT substr(source_session, 9), MAX(created_at) FROM notes WHERE kind='summary' "
                "AND source_session LIKE 'summary:%' GROUP BY source_session"))
        return [(r[0], r[1]) for r in rows if r[0] not in made or r[2] > made[r[0]]]

    def entity_notes(self, entity: str, limit: int = 20) -> list[dict]:
        """Newest *limit* active non-summary notes of an entity, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT n.id, n.content, n.created_at FROM notes n JOIN note_entities ne ON ne.note_id=n.id "
                "JOIN entities e ON e.id=ne.entity_id WHERE e.name=? AND n.status='active' AND n.review='active' AND n.kind!='summary' "
                "ORDER BY n.created_at DESC, n.id DESC LIMIT ?", (entity, limit)).fetchall()
        return [dict(id=r[0], content=r[1], created_at=r[2]) for r in reversed(rows)]

    # -- semantic vectors (optional; computed by vec.py / embed_server.py, stored here as float32 BLOBs) ------------

    def missing_vectors(self, model: str, limit: int = 32) -> list[tuple[int, str]]:
        """(id, text) of active notes that have no vector for *model* yet, newest first."""
        with self._lock:
            return [(r[0], r[1][:500]) for r in self.conn.execute(
                "SELECT n.id, n.content FROM notes n LEFT JOIN vectors v ON v.note_id=n.id AND v.model=? "
                "WHERE n.status='active' AND v.note_id IS NULL ORDER BY n.id DESC LIMIT ?", (model, limit))]

    def put_vectors(self, model: str, items: Iterable[tuple[int, bytes]]) -> int:
        rows = [(i, model, sqlite3.Binary(b)) for i, b in items]
        with self._lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO vectors(note_id, model, vec) VALUES (?,?,?)", rows)
        return len(rows)

    def notes_by_ids(self, ids: Iterable[int], *, kind: str | None = None, since=None, until=None,
                     scope: str | None = None, tags: str | Iterable[str] = "", now: float | None = None,
                     include_low_trust: bool = False, as_of=None) -> list[dict]:
        """Search-result dicts for *ids* (order kept) after the same hard filters search() applies: active, not expired,
        kind, date window, trust, scope, tags. Used to admit vector-only hits next to the lexical ones."""
        now = _now() if now is None else now
        s_since, s_until = _parse_when(since), _parse_when(until)
        window = None
        if s_since is not None or s_until is not None:
            window = (s_since if s_since is not None else 0.0, (s_until + DAY) if s_until is not None else 1e18)
        want_tags = _clean_tags(tags).split()
        as_end = day_bounds(as_of)[1] if as_of not in (None, "") else None
        with self._lock:
            scopes = self._scopes(scope)
            info = self._fetch(list(ids))
        out = []
        for i in ids:
            r = info.get(i)
            if r is None or r["status"] not in (("active", "superseded") if as_end is not None else ("active",)):
                continue
            if as_end is not None and not valid_at(r, as_end):
                continue
            if r["expires_at"] is not None and r["expires_at"] <= now and not r["pinned"]:
                continue
            if kind and r["kind"] != kind:
                continue
            if window and not (window[0] <= (r["event_at"] or r["created_at"]) < window[1]):
                continue
            if not include_low_trust and r["trust"] < TRUST_HIDE_BELOW:
                continue
            if scopes is not None and r["scope"] not in scopes:
                continue
            if want_tags and not set(want_tags) <= set(r["tags"].split()):
                continue
            out.append(self._row(r) | {"score": 0.0})
        return out

    def close(self) -> None:
        with self._lock:
            self.flush_access()
            self.conn.close()

    def flush_access(self, now: float | None = None) -> int:
        now = _now() if now is None else now
        with self._lock:
            buf, self._access_buf = self._access_buf, Counter()
            self._access_flushed = now
            if buf:
                with self.conn:
                    self.conn.executemany("UPDATE notes SET access_count=access_count+?, last_access=? WHERE id=?",
                                          [(n, now, i) for i, n in buf.items()])
            return len(buf)

    def _note_access(self, ids: Iterable[int], now: float) -> None:
        if not self.track_access:
            return
        self._access_buf.update(ids)
        if now - self._access_flushed >= ACCESS_FLUSH_SECONDS:
            self.flush_access(now)
