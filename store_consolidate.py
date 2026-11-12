"""DecisionStore mixin: maintenance, near-duplicate merge, supersede, restore, history."""

from __future__ import annotations

import sqlite3
import time
from functools import lru_cache
from collections import defaultdict
from typing import Iterable

try:
    from .store_common import (
        DAY, GENERIC_ENTITIES, HOT_HALF_LIFE_DAYS, IDLE_DECAY, PROGRESS_DECAY, RE_PROGRESS, MERGE_CONTAINMENT, MERGE_JACCARD,
        MERGE_JACCARD_ANY_KIND, SUPERSEDE_JACCARD, _now, _set_sim, default_expiry, normalize, numbers,
        sim_features, slot_values
    )
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import (
        DAY, GENERIC_ENTITIES, HOT_HALF_LIFE_DAYS, IDLE_DECAY, PROGRESS_DECAY, RE_PROGRESS, MERGE_CONTAINMENT, MERGE_JACCARD,
        MERGE_JACCARD_ANY_KIND, SUPERSEDE_JACCARD, _now, _set_sim, default_expiry, normalize, numbers,
        sim_features, slot_values
    )


CANDIDATE_MAX_DF = 300      # ignore terms present in more notes than this when looking for similar notes
CONSOLIDATE_BATCH = 500     # commit (and release the lock) every this many notes
# maintenance() runs when a session starts (plugin) or at the first write (MCP server), so it must not hold them up: its
# consolidation stops after this many seconds (once the current batch is done) and the next session goes on from there.
# An already consolidated database needs a fraction of a second; a first run over a large import spreads over sessions.
MAINTENANCE_BUDGET = 10.0


@lru_cache(maxsize=50000)
def _numbers_cached(text: str):
    return frozenset(numbers(text))


class ConsolidateMixin:
    def maintenance(self, now: float | None = None, consolidate: bool = True,
                    time_budget: float | None = MAINTENANCE_BUDGET) -> int:
        """Purge expired, unpinned notes (returns how many) and consolidate new notes, for at most *time_budget*
        seconds after the first batch (None: no limit; the rest is done by the next call). Search never deletes."""
        now = _now() if now is None else now
        self.flush_access(now)
        with self._lock, self.conn:
            # hot notes (recalled within the last half-life) get one more TTL period instead of being purged
            for r in self.conn.execute(
                    "SELECT id, kind, expires_at FROM notes WHERE expires_at IS NOT NULL AND expires_at <= ? AND "
                    "pinned=0 AND last_access IS NOT NULL AND last_access > ?",
                    (now, now - HOT_HALF_LIFE_DAYS * DAY)).fetchall():
                if default_expiry(r["kind"], now):
                    self.conn.execute("UPDATE notes SET expires_at=? WHERE id=?",
                                      (default_expiry(r["kind"], now), r["id"]))
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM notes WHERE expires_at IS NOT NULL AND expires_at <= ? AND pinned=0", (now,))]
            self._delete_ids(ids)
        if consolidate:
            self.consolidate(time_budget=time_budget)
        return len(ids)

    # -- consolidation ------------------------------------------------------------------------------------------

    def _similar_candidates(self, nid: int, dfs: dict, limit_terms: int = 8) -> list[int]:
        """Notes sharing the rarest content terms of *nid* (SQL-side candidate generation)."""
        mine = [r[0] for r in self.conn.execute(
            "SELECT term FROM postings WHERE note_id=? AND field=0 AND length(term) > 1", (nid,))]
        # terms in more than CANDIDATE_MAX_DF notes say little about similarity and make the posting scan quadratic
        terms = sorted((t for t in mine if 1 < dfs.get(t, 0) <= CANDIDATE_MAX_DF), key=lambda t: dfs[t])[:limit_terms]
        if not terms:
            return []
        q = ",".join("?" * len(terms))
        return [r[0] for r in self.conn.execute(
            f"SELECT p.note_id FROM postings p CROSS JOIN notes n ON n.id=p.note_id WHERE p.term IN ({q}) AND p.field=0 "
            f"AND p.note_id != ? AND n.status='active' AND n.locked=0 GROUP BY p.note_id "
            f"HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC LIMIT 40",
            (*terms, nid, max(1, min(3, len(terms) // 2))))]

    def consolidate(self, full: bool = False, now: float | None = None, time_budget: float | None = None,
                    batch: int = CONSOLIDATE_BATCH) -> dict:
        """Cluster near-duplicate notes and supersede outdated state notes. Never deletes: merged notes get
        status='merged' (canonical_id -> kept note), outdated ones status='superseded'; both are hidden from search
        by default (their matches are credited to the canonical note) and can be restored. The kept note is the
        most informative one (most distinct features, then newest) and accumulates proof_count.

        Work is committed every *batch* notes and the lock is released in between, so readers and writers are not
        blocked for the whole run. With *time_budget* (seconds) it stops after the current batch once the budget is
        spent (at least one batch always runs, so repeated calls always make progress); the unfinished notes keep consolidated_at NULL and the next call continues from there
        (stats['remaining'] says how many are left)."""
        now = _now() if now is None else now
        t0 = time.monotonic()
        stats = {"examined": 0, "merged": 0, "superseded": 0}
        with self._lock, self.conn:
            if full:
                self.conn.execute("UPDATE notes SET consolidated_at=NULL WHERE status='active'")
            todo = [r[0] for r in self.conn.execute(
                "SELECT id FROM notes WHERE consolidated_at IS NULL AND status='active' AND locked=0 ORDER BY id")]
            dfs = dict(self.conn.execute(
                "SELECT term, COUNT(*) FROM postings WHERE field=0 AND length(term) > 1 GROUP BY term")) if todo else {}
        cache: dict[int, sqlite3.Row] = {}
        feats: dict[int, frozenset] = {}
        ents_cache: dict[int, set] = {}

        def info(i: int):
            if i not in cache:
                cache[i] = self.conn.execute("SELECT * FROM notes WHERE id=?", (i,)).fetchone()
                feats[i] = sim_features(cache[i]["content"]) if cache[i] else frozenset()
            return cache[i]

        def ents_of(i: int):
            if i not in ents_cache:
                ents_cache[i] = {r[0] for r in self.conn.execute(
                    "SELECT entity_id FROM note_entities WHERE note_id=?", (i,))}
            return ents_cache[i]

        done = 0
        for lo in range(0, len(todo), max(1, batch)):
            if lo and time_budget is not None and time.monotonic() - t0 >= time_budget:
                break   # always finish one batch, so even a tiny budget makes progress
            chunk = todo[lo:lo + max(1, batch)]
            with self._lock, self.conn:
                for nid in chunk:
                    me = info(nid)
                    if me is None or me["status"] != "active":
                        continue
                    stats["examined"] += 1
                    best = None
                    for cid in self._similar_candidates(nid, dfs):
                        other = info(cid)
                        if other is None or other["status"] != "active" or other["review"] != me["review"]:
                            continue   # a pending note is merged only after it is confirmed (it must not hide a confirmed one)
                        j, c = _set_sim(feats[nid], feats[cid])
                        if j < SUPERSEDE_JACCARD and c < MERGE_CONTAINMENT:
                            continue
                        rel = self._relation(me, other, feats[nid], feats[cid], j, c, ents_of)
                        if rel and (best is None or j > best[0]):
                            best = (j, cid, rel)
                    if best:
                        _, cid, (rel, keep, drop) = best
                        self._fold(drop, keep, rel, now)
                        stats[rel] += 1
                        for i in (keep, drop):
                            cache.pop(i, None)
                            ents_cache.pop(i, None)
                    self.conn.execute("UPDATE notes SET consolidated_at=? WHERE id=?", (now, nid))
            done += len(chunk)
            time.sleep(0.002)   # Python locks are not fair: give a waiting reader/writer the chance to take the lock
            if len(cache) > 20000:   # bound memory on very large databases
                cache.clear(); feats.clear(); ents_cache.clear()
        stats["remaining"] = len(todo) - done
        stats["stale"] = self.mark_stale()
        stats["hidden_total"] = self.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE status!='active'").fetchone()[0]
        return stats

    def mark_stale(self) -> int:
        """Soft supersede for capacity facts. For each attribute (swap/ram) the newest active state note that
        states a size is current; older state notes sharing an entity with it and stating a different size get a
        'stale_by' link and a ranking penalty in search. Nothing is hidden, so the other facts in a mixed note
        (firewall, CPU, ...) stay searchable. Recomputed from scratch on every call."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM note_links WHERE rel='stale_by'")
            rows = self.conn.execute(
                "SELECT id, content, COALESCE(event_at, created_at) AS t FROM notes "
                "WHERE status='active' AND kind='state' ORDER BY t, id").fetchall()
            by_attr: dict[str, list] = defaultdict(list)
            for r in rows:
                for attr, mb in slot_values(r["content"]).items():
                    by_attr[attr].append((r["t"], r["id"], mb))
            ents: dict[int, set] = {}

            def ents_of(i: int) -> set:
                if i not in ents:
                    ents[i] = {x[0] for x in self.conn.execute(
                        "SELECT ne.entity_id, e.name FROM note_entities ne JOIN entities e ON e.id=ne.entity_id "
                        "WHERE ne.note_id=?", (i,)) if normalize(x[1]) not in GENERIC_ENTITIES}
                return ents[i]

            stale: dict[int, int] = {}
            for items in by_attr.values():
                latest_t, latest_id, latest_mb = items[-1]
                for t, i, mb in items[:-1]:
                    if abs(mb - latest_mb) > 0.1 * max(mb, latest_mb) and latest_t - t >= 3600 and ents_of(i) & ents_of(latest_id):
                        stale[i] = latest_id  # later attributes are newer or equal, so the last write wins
            now = _now()
            self.conn.executemany("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,'stale_by',?)",
                                  [(i, j, now) for i, j in stale.items()])
        return len(stale)

    @staticmethod
    def _idle_factor(r, now: float) -> float:
        """Soft decay for notes nobody has recalled for a long time (see IDLE_DECAY). 1.0 for exempt kinds/pinned."""
        spec = IDLE_DECAY.get(r["kind"])
        if spec is None or r["pinned"]:
            return 1.0
        floor, half = spec
        if r["kind"] == "state" and RE_PROGRESS.search(r["content"] or ""):
            floor, half = PROGRESS_DECAY
        touched = max(r["last_access"] or 0.0, r["event_at"] or r["created_at"] or 0.0)
        return floor + (1.0 - floor) * 0.5 ** (max(0.0, now - touched) / DAY / half)

    def stale_ids(self, ids: Iterable[int]) -> dict[int, int]:
        ids = list(ids)
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r[0]: r[1] for r in self.conn.execute(
            f"SELECT src, dst FROM note_links WHERE rel='stale_by' AND src IN ({q})", ids)}

    @staticmethod
    def _nums(row) -> frozenset:
        return _numbers_cached(row["content"])

    def _relation(self, a, b, fa, fb, j, c, ents_of):
        """('merged'|'superseded', keep_id, drop_id) or None for two active notes."""
        na, nb = self._nums(a), self._nums(b)
        shared_ents = bool(ents_of(a["id"]) & ents_of(b["id"]))
        same_kind = a["kind"] == b["kind"]
        ta = a["event_at"] or a["created_at"]
        tb = b["event_at"] or b["created_at"]
        conflict = bool(na and nb and na != nb and not (na <= nb or nb <= na))
        if not conflict:
            near_dup = (j >= MERGE_JACCARD and same_kind) or j >= MERGE_JACCARD_ANY_KIND
            subsumed = (c >= MERGE_CONTAINMENT and min(len(fa), len(fb)) >= 6 and (same_kind or j >= 0.5)
                        and (shared_ents or j >= 0.45))
            if near_dup or subsumed:
                small, big = (a, b) if len(fa) < len(fb) or (len(fa) == len(fb) and ta < tb) else (b, a)
                # The kept note must contain every quantity of the hidden one.
                if self._nums(small) <= self._nums(big):
                    return "merged", big["id"], small["id"]
            return None
        if a["kind"] == "state" and b["kind"] == "state" and j >= SUPERSEDE_JACCARD and shared_ents \
                and abs(ta - tb) >= 3600:
            newer, older = (a, b) if ta > tb else (b, a)
            return "superseded", newer["id"], older["id"]
        return None

    def _fold(self, drop: int, keep: int, rel: str, now: float) -> None:
        """Hide *drop* behind *keep* (and re-point anything that was hidden behind *drop*)."""
        status = "merged" if rel == "merged" else "superseded"
        link = "merged_into" if rel == "merged" else "superseded_by"
        proof = self.conn.execute("SELECT proof_count FROM notes WHERE id=?", (drop,)).fetchone()[0]
        self.conn.execute("UPDATE notes SET status=?, canonical_id=? WHERE id=?", (status, keep, drop))
        self.conn.execute("UPDATE notes SET canonical_id=? WHERE canonical_id=?", (keep, drop))
        if rel == "merged":
            self.conn.execute("UPDATE notes SET proof_count=proof_count+? WHERE id=?", (proof, keep))
        self.conn.execute("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,?,?)",
                          (drop, keep, link, now))
        if rel != "merged":
            # bi-temporal validity: the older fact stops being true when the newer one took effect
            k = self.conn.execute("SELECT event_at, created_at FROM notes WHERE id=?", (keep,)).fetchone()
            when = (k["event_at"] or k["created_at"]) if k else now
            self.conn.execute("UPDATE notes SET valid_to=COALESCE(valid_to, ?) WHERE id=?", (when, drop))
            self.conn.execute("UPDATE notes SET valid_from=COALESCE(valid_from, ?) WHERE id=?", (when, keep))

    def fold_notes(self, drop: int, keep: int, rel: str, now: float | None = None) -> bool:
        """Hide *drop* behind *keep* on request (the sleep-time review): rel is 'merged' or 'superseded', with the same
        effect as consolidate() - reversible with restore(), valid_to / valid_from set for a supersession. Refuses
        (False) when either note is not active, they are the same note, or *drop* is pinned or locked."""
        if rel not in ("merged", "superseded") or drop == keep:
            return False
        now = _now() if now is None else now
        with self._lock, self.conn:
            rows = {r["id"]: r for r in self.conn.execute("SELECT id, status, pinned, locked FROM notes WHERE id IN (?,?)",
                                                          (drop, keep))}
            if len(rows) != 2 or any(r["status"] != "active" for r in rows.values()):
                return False
            if rows[drop]["pinned"] or rows[drop]["locked"]:
                return False
            self._fold(drop, keep, rel, now)
            return True

    def restore(self, note_id: int) -> bool:
        """Undo a merge/supersede: the note becomes active again and is excluded from future consolidation."""
        with self._lock, self.conn:
            r = self.conn.execute("SELECT status, canonical_id, proof_count FROM notes WHERE id=?",
                                  (note_id,)).fetchone()
            if not r:
                return False
            if r["status"] == "rejected":
                return self.restore_rejected(note_id)
            if r["status"] == "merged" and r["canonical_id"]:
                self.conn.execute("UPDATE notes SET proof_count=MAX(1, proof_count-?) WHERE id=?",
                                  (r["proof_count"], r["canonical_id"]))
            if r["status"] == "superseded" and r["canonical_id"]:
                # undo the validity end that _fold set (only if it is still that value; a user-set valid_to stays)
                k = self.conn.execute("SELECT event_at, created_at FROM notes WHERE id=?",
                                      (r["canonical_id"],)).fetchone()
                if k:
                    self.conn.execute("UPDATE notes SET valid_to=NULL WHERE id=? AND ABS(valid_to - ?) < 1",
                                      (note_id, k["event_at"] or k["created_at"]))
            self.conn.execute("UPDATE notes SET status='active', canonical_id=NULL, locked=1 WHERE id=?", (note_id,))
            self.conn.execute("DELETE FROM note_links WHERE src=? AND rel IN ('merged_into','superseded_by')",
                              (note_id,))
            return True

    def history(self, note_id: int, depth: int = 5) -> list[dict]:
        """Notes hidden behind *note_id* (merged duplicates and superseded older versions), newest first."""
        with self._lock:
            out, frontier, seen = [], [note_id], {note_id}
            for _ in range(depth):
                if not frontier:
                    break
                q = ",".join("?" * len(frontier))
                rows = self.conn.execute(f"SELECT l.rel, n.* FROM note_links l JOIN notes n ON n.id=l.src "
                                         f"WHERE l.dst IN ({q})", frontier).fetchall()
                frontier = []
                for r in rows:
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        frontier.append(r["id"])
                        d = self._row(r)
                        d["rel"] = r["rel"]
                        out.append(d)
            out.sort(key=lambda d: d["created_at"], reverse=True)
            return out
