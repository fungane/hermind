"""DecisionStore mixin: deterministic core summary of pinned notes and the most trusted preferences.

No LLM. The provider puts this block in the system prompt of every session, so the user's standing rules are always
in view without a search. The text is cached and rebuilt only after the database changed (this connection's writes via
``total_changes``, other connections' commits via ``PRAGMA data_version``) or the hour rolled over (expiry)."""

from __future__ import annotations

try:
    from .screen import looks_masked_secret
    from .store_common import IMPORT_HERMIND, IMPORT_PREFIX, TRUST_DEFAULT, TRUST_HIDE_BELOW, _clip, _now, core_text, defang_fence_tags, looks_secret
except ImportError:  # loaded as a top-level module (tools/)
    from screen import looks_masked_secret
    from store_common import IMPORT_HERMIND, IMPORT_PREFIX, TRUST_DEFAULT, TRUST_HIDE_BELOW, _clip, _now, core_text, defang_fence_tags, looks_secret

CORE_DEFAULT_CHARS = 3500
CORE_LINE_CHARS = 220
CORE_HEADER = "Core memory (pinned notes and the user's most trusted preferences; use memory_search for anything else):"


def _fit(lines: list[str], budget: int) -> tuple[list[str], int]:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    return out, len(lines) - len(out)


class CoreMixin:
    def _core_lines(self, rows) -> list[str]:
        lines, seen = [], set()
        for r in rows:
            text = defang_fence_tags(_clip(core_text(r["content"]) or r["content"], CORE_LINE_CHARS))
            if not text or text in seen or looks_secret(r["content"]) or looks_secret(text) \
                    or looks_masked_secret(text):
                continue
            seen.add(text)
            lines.append(f"- {text}")
        return lines

    def _build_core(self, max_chars: int, scope: str | None, now: float) -> str:
        sc = self._scopes(scope)
        scope_sql, args = "", []
        if sc is not None:
            scope_sql = f" AND scope IN ({','.join('?' * len(sc))})"
            args = list(sc)
        base = ("SELECT * FROM notes WHERE status='active' AND review='active' AND candidate=0 AND kind!='summary' "
                f"AND trust >= ?{scope_sql}")
        with self._lock:
            pinned = self.conn.execute(base + " AND pinned=1 ORDER BY trust DESC, created_at DESC, id DESC LIMIT 200",
                                       [TRUST_HIDE_BELOW, *args]).fetchall()
            prefs = self.conn.execute(
                base + " AND pinned=0 AND kind='preference' AND (expires_at IS NULL OR expires_at > ?) "
                "AND trust >= ? "
                # a preference imported from another tool stays out until it is used: recalled, pinned (the pinned part is
                # separate) or trusted above the default by feedback
                "AND NOT (substr(source_session, 1, ?) = ? AND source_session != ? AND COALESCE(access_count, 0) = 0 "
                "AND trust <= ?) "
                "ORDER BY trust DESC, proof_count DESC, created_at DESC, id DESC LIMIT 200",
                [TRUST_HIDE_BELOW, *args, now, TRUST_DEFAULT, len(IMPORT_PREFIX), IMPORT_PREFIX, IMPORT_HERMIND,
                 TRUST_DEFAULT]).fetchall()
        pin_lines, pref_lines = self._core_lines(pinned), self._core_lines(prefs)
        pin_set = set(pin_lines)
        pref_lines = [x for x in pref_lines if x not in pin_set]
        if not pin_lines and not pref_lines:
            return ""
        reserve = 80   # room for the "(+N more ...)" footer
        budget = max_chars - len(CORE_HEADER) - 1 - reserve - (len("Pinned:") + len("Preferences:") + 2)
        # pinned notes first, but a long pinned list never crowds out the preferences entirely (they keep >= 1/3)
        pin_budget = budget if not pref_lines else max(budget * 2 // 3, budget - sum(len(x) + 1 for x in pref_lines))
        pin_out, pin_more = _fit(pin_lines, max(0, pin_budget))
        used = sum(len(x) + 1 for x in pin_out)
        pref_out, pref_more = _fit(pref_lines, max(0, budget - used))
        parts = [CORE_HEADER]
        if pin_out:
            parts += ["Pinned:", *pin_out]
        if pref_out:
            parts += ["Preferences:", *pref_out]
        more = pin_more + pref_more
        if more:
            parts.append(f"(+{more} more; memory_list / memory_search)")
        body = "\n".join(parts)
        if len(body) > max_chars:
            body = body[:max_chars - 1] + "…"
        if looks_secret(body) or looks_masked_secret(body):   # belt and braces: fail closed
            return ""
        return body

    def core_summary(self, max_chars: int = CORE_DEFAULT_CHARS, scope: str | None = None,
                     now: float | None = None) -> str:
        """The core block ('' when there is nothing to show), at most *max_chars* characters. Deterministic."""
        max_chars = int(max_chars or 0)
        if max_chars < 300:
            return ""
        now = _now() if now is None else now
        with self._lock:
            stamp = (self.conn.total_changes, self.conn.execute("PRAGMA data_version").fetchone()[0], int(now // 3600))
            cache = self.__dict__.setdefault("_core_cache", {})
            key = (max_chars, scope or self.scope)
            hit = cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            text = self._build_core(max_chars, scope, now)
            cache[key] = (stamp, text)
            return text
