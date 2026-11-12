"""DecisionStore mixin: review queue for LLM-extracted notes.

A note written by LLM extraction (capture_extract) starts with review='pending': searchable but ranked x0.6 and marked
[pending]. The user (through the memory_review tool) confirms it (-> review='active') or rejects it. Rejecting is a
soft forget: the note stays in the database with status='rejected' (hidden from every read), its digest is
tombstoned so auto-capture cannot recreate it, and restore_rejected() undoes both."""

from __future__ import annotations

from typing import Iterable

try:
    from .store_common import _digest, _now
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import _digest, _now


class ReviewMixin:
    def list_pending(self, limit: int = 50, scope: str | None = None, offset: int = 0) -> list[dict]:
        """Notes waiting for review, oldest first (the order they were extracted in)."""
        sql, args = "SELECT * FROM notes WHERE status='active' AND review='pending'", []
        sc = self._scopes(scope)
        if sc is not None:
            sql += f" AND scope IN ({','.join('?' * len(sc))})"
            args += sc
        sql += " ORDER BY id LIMIT ? OFFSET ?"
        args += [max(1, min(int(limit or 50), 500)), max(0, int(offset or 0))]
        with self._lock:
            return [self._row(r) for r in self.conn.execute(sql, args)]

    def pending_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes WHERE status='active' AND review='pending'"
                                     ).fetchone()[0]

    def list_rejected(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self.conn.execute(
                "SELECT * FROM notes WHERE status='rejected' ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))]

    def confirm(self, note_id: int) -> bool:
        """pending -> active. The note becomes eligible for consolidation again. False if it is not pending."""
        with self._lock, self.conn:
            cur = self.conn.execute("UPDATE notes SET review='active', consolidated_at=NULL "
                                    "WHERE id=? AND status='active' AND review='pending'", (note_id,))
            return bool(cur.rowcount)

    def mark_pending(self, note_id: int) -> bool:
        """active -> pending: a note whose text was changed by a writer the maintainer does not trust goes back to the
        review queue. False if the note is not an active, confirmed note."""
        with self._lock, self.conn:
            cur = self.conn.execute("UPDATE notes SET review='pending' WHERE id=? AND status='active' AND review='active'",
                                    (note_id,))
            return bool(cur.rowcount)

    def reject(self, note_id: int, now: float | None = None) -> bool:
        """pending -> rejected (soft forget, restorable). Only a note that is still pending can be rejected; a
        confirmed note is removed with forget(). False if there is nothing to reject."""
        now = _now() if now is None else now
        with self._lock, self.conn:
            r = self.conn.execute("SELECT content FROM notes WHERE id=? AND status='active' AND review='pending'",
                                  (note_id,)).fetchone()
            if not r:
                return False
            self.conn.execute("UPDATE notes SET status='rejected', canonical_id=NULL WHERE id=?", (note_id,))
            self.conn.execute("INSERT OR IGNORE INTO tombstones(digest, note_id, created_at) VALUES (?,?,?)",
                              (_digest(r["content"]), note_id, now))
            return True

    def restore_rejected(self, note_id: int) -> bool:
        """rejected -> pending again (back in the queue), and the tombstone made for the rejection is lifted."""
        with self._lock, self.conn:
            r = self.conn.execute("SELECT content FROM notes WHERE id=? AND status='rejected'", (note_id,)).fetchone()
            if not r:
                return False
            self.conn.execute("UPDATE notes SET status='active', review='pending', consolidated_at=NULL WHERE id=?",
                              (note_id,))
            self.conn.execute("DELETE FROM tombstones WHERE digest=? AND note_id=?", (_digest(r["content"]), note_id))
            return True

    def review_many(self, action: str, ids: Iterable[int]) -> dict:
        """confirm / reject / restore several notes; returns {'done': [ids], 'skipped': [ids]}."""
        fn = {"confirm": self.confirm, "reject": self.reject, "restore": self.restore_rejected}[action]
        done, skipped = [], []
        for i in dict.fromkeys(int(x) for x in ids):
            (done if fn(i) else skipped).append(i)
        return {"done": done, "skipped": skipped}
