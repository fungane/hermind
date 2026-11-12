"""Personal-data detector and redactor for text that is about to leave the machine (pure stdlib).

Used by llm.py: before a prompt goes to the configured LLM endpoint, email addresses, phone numbers, ID-card numbers
and payment-card numbers are replaced by a tag ("[email]") or the call is skipped. Notes in the database are NOT
changed. Detection is pattern- and checksum-based, so it is conservative about what it calls a number and will miss
free-form personal data (names, street addresses, medical details). `extra` takes the user's own regexes.
"""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = {0x200B: None, 0x200C: None, 0x200D: None, 0x2060: None, 0xFEFF: None}

# the last label must be letters (a TLD): `ubuntu@203.0.113.7` is an SSH target, not a mailbox
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?![\w-])")
_HKID = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2})(\d{6})\s?\(?([0-9A])\)?(?![A-Za-z0-9])")
_CNID = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
# a card number is 13-19 contiguous digits or groups of 4 (4-6-5 for Amex) split by one space or hyphen; this skips
# timestamps like 20261030-180933 and DOIs, which a plain "digits with separators" pattern mistook for cards
_CARD = re.compile(r"(?<![\d/.\-])(\d{13,19}|\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?|\d{4}[ -]\d{6}[ -]\d{5})(?![\d/.\-]?\d)")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\s?\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d){5,11}(?!\d)")
_PHONE_HK = re.compile(r"(?<![\d+])(?:\(?852\)?[ -]?)[2-9]\d{3}[ -]?\d{4}(?!\d)")
_PHONE_CN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# a bare 8-digit Hong Kong number only counts next to a word that says it is a phone number
_PHONE_BARE = re.compile(r"(?i)(?:電話|手提|手機|聯絡|聯繫|tel|phone|mobile|whatsapp|call|致電)[^\d\n]{0,12}([2-9]\d{3}[ -]?\d{4})(?!\d)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _hkid_ok(letters: str, digits: str, check: str) -> bool:
    vals = [ord(c) - 55 for c in letters]                    # A=10 .. Z=35
    if len(vals) == 1:
        vals = [36] + vals                                   # a blank first letter counts as 36
    weights = [9, 8]
    total = sum(v * w for v, w in zip(vals, weights)) + sum(int(d) * w for d, w in zip(digits, range(7, 1, -1)))
    want = (11 - total % 11) % 11
    return check == ("A" if want == 10 else str(want))


def _cnid_ok(s: str) -> bool:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    total = sum(int(c) * w for c, w in zip(s[:17], weights))
    return "10X98765432"[total % 11] == s[17].upper() and 1900 <= int(s[6:10]) <= 2100


def _spans(text: str):
    """(start, end, kind) for every detected item; later passes skip anything already covered."""
    found: list[tuple[int, int, str]] = []

    def free(a: int, b: int) -> bool:
        return not any(a < e and s < b for s, e, _ in found)

    def add(m, kind, group=0):
        a, b = m.span(group)
        if free(a, b):
            found.append((a, b, kind))

    for m in _EMAIL.finditer(text):
        add(m, "email")
    for m in _CNID.finditer(text):
        if _cnid_ok(m.group(1)):
            add(m, "id")
    for m in _HKID.finditer(text):
        if _hkid_ok(m.group(1), m.group(2), m.group(3)):
            add(m, "id")
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            add(m, "card")
    for rx in (_PHONE_HK, _PHONE_INTL, _PHONE_CN):
        for m in rx.finditer(text):
            add(m, "phone")
    for m in _PHONE_BARE.finditer(text):
        add(m, "phone", 1)
    return found


def find_pii(text: str, extra=()) -> list[str]:
    """Kinds of personal data found in *text* (sorted, unique); [] when none."""
    t = _norm(text)
    kinds = {k for _, _, k in _spans(t)}
    for rx in _compile(extra):
        if rx.search(t):
            kinds.add("custom")
    return sorted(kinds)


def redact_pii(text: str, extra=()) -> tuple[str, dict]:
    """(text with every detected item replaced by "[kind]", {kind: count}). *text* is returned unchanged when clean."""
    t = _norm(text)
    spans = _spans(t)
    for rx in _compile(extra):
        for m in rx.finditer(t):
            if m.end() > m.start() and not any(m.start() < e and s < m.end() for s, e, _ in spans):
                spans.append((m.start(), m.end(), "custom"))
    if not spans:
        return text, {}
    counts: dict = {}
    out, pos = [], 0
    for a, b, kind in sorted(spans):
        out.append(t[pos:a])
        out.append(f"[{kind}]")
        counts[kind] = counts.get(kind, 0) + 1
        pos = b
    out.append(t[pos:])
    return "".join(out), counts


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", (text or "").translate(_ZERO_WIDTH))


def _compile(extra) -> list:
    out = []
    for p in extra or ():
        try:
            out.append(re.compile(str(p)))
        except re.error:
            continue                                         # a bad user regex must not break every LLM call
    return out
