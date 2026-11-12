"""DecisionStore mixin: open-rate bookkeeping for the ranking prior.

A note counts as *shown* when a search or the automatic recall listed it, and as *opened* when the agent then fetched
it with memory_get in the same session. Counts live on the note (shown_count, opened_count, fb_at) as decayed floats:
every update first fades the old counts by 0.5 ** (age / 90 days). store_search turns them into a bounded multiplier
(see store_common.open_rate_factor): at most +-10 %, only after the note was shown several times."""

from __future__ import annotations

import time
from typing import Iterable

try:
    from .store_common import FB_MAX_EFFECT, FB_MIN_SHOWN, _now, fb_decay, l0, open_rate_factor
except ImportError:  # loaded as a top-level module (tools/)
    from store_common import FB_MAX_EFFECT, FB_MIN_SHOWN, _now, fb_decay, l0, open_rate_factor

_BASE_TTL = 60.0


class FeedbackMixin:
    def _bump_feedback(self, ids: Iterable[int], opened: bool, now: float) -> int:
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            return 0
        n = 0
        with self._lock, self.conn:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                rows = self.conn.execute(
                    f"SELECT id, shown_count, opened_count, fb_at FROM notes WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
                for r in rows:
                    d = fb_decay(r["fb_at"], now)
                    shown, op = r["shown_count"] * d, r["opened_count"] * d
                    if opened:
                        op = min(op + 1.0, max(shown, 1.0))
                        shown = max(shown, op)
                    else:
                        shown += 1.0
                    self.conn.execute("UPDATE notes SET shown_count=?, opened_count=?, fb_at=? WHERE id=?",
                                      (shown, op, now, r["id"]))
                    n += 1
        self.__dict__.pop("_fb_base", None)
        return n

    def record_shown(self, ids: Iterable[int], now: float | None = None) -> int:
        """These notes were listed to the agent (once per session and note: the caller dedupes). Never raises."""
        try:
            return self._bump_feedback(ids, False, _now() if now is None else now)
        except Exception:
            return 0

    def record_opened(self, note_id: int, now: float | None = None) -> bool:
        """The agent opened this note with memory_get after it had been shown. Never raises."""
        try:
            return bool(self._bump_feedback([note_id], True, _now() if now is None else now))
        except Exception:
            return False

    def open_rate_base(self) -> float:
        """Corpus-wide smoothed open rate (the neutral point of the prior); cached for a minute."""
        hit = self.__dict__.get("_fb_base")
        if hit is not None and time.monotonic() - hit[0] < _BASE_TTL:
            return hit[1]
        with self._lock:
            o, s = self.conn.execute("SELECT COALESCE(SUM(opened_count),0), COALESCE(SUM(shown_count),0) FROM notes "
                                     "WHERE shown_count > 0").fetchone()
        base = (o + 1.0) / (s + 5.0)
        self.__dict__["_fb_base"] = (time.monotonic(), base)
        return base

    def open_rate_report(self, top: int = 5, now: float | None = None) -> dict:
        """Totals and the most boosted / most demoted notes, for tools/usage_report.py."""
        now = _now() if now is None else now
        with self._lock:
            rows = self.conn.execute("SELECT id, content, kind, shown_count, opened_count, fb_at FROM notes "
                                     "WHERE shown_count > 0").fetchall()
        self.__dict__.pop("_fb_base", None)
        base = self.open_rate_base() if rows else 0.0
        items = []
        for r in rows:
            f = open_rate_factor(r["shown_count"], r["opened_count"], r["fb_at"], now, base)
            d = fb_decay(r["fb_at"], now)
            items.append({"id": r["id"], "kind": r["kind"], "shown": round(r["shown_count"] * d, 2),
                          "opened": round(r["opened_count"] * d, 2), "factor": round(f, 3),
                          "text": l0(r["content"], 50)})
        active = [x for x in items if x["factor"] != 1.0]
        return {"tracked": len(items), "shown_total": round(sum(r["shown_count"] for r in rows), 1),
                "opened_total": round(sum(r["opened_count"] for r in rows), 1), "base_rate": round(base, 3),
                "min_shown": FB_MIN_SHOWN, "max_effect": FB_MAX_EFFECT, "with_effect": len(active),
                "boosted": sorted((x for x in active if x["factor"] > 1.0), key=lambda x: -x["factor"])[:top],
                "demoted": sorted((x for x in active if x["factor"] < 1.0), key=lambda x: x["factor"])[:top]}
