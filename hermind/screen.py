"""Write-side screens for hermind (pure stdlib).

* looks_secret(): conservative credential detector (after Holographic's text screen). A note that looks like it
  contains a password / API key / private key is refused rather than stored.
* looks_masked_secret(): a credential already masked upstream (sk-pro...abcd, user:***@); explicit writes refuse
  it because the stored note would carry no value, only clutter.
* clean_prompt(): the user's own words in a prompt (platform notices dropped, attachment markers removed); used
  for automatic recall.
* strip_injected(): remove recalled-memory blocks (our own <hermind-context> wrapper and other providers'
  *-memory/*-context tags) and inline data URIs from captured chat, so recalled notes are not re-stored as new ones.
"""

from __future__ import annotations

import re
import unicodedata

CONTEXT_TAG = "hermind-context"

_CRED_WORD = (r"(?:passwords?|passwd|pwd|pass\s*phrase|passphrase|(?:access|auth|api|bot|bearer|refresh|secret)?[_\- ]?"
              r"tokens?|api[_\- ]?keys?|secret(?:[_\- ]?keys?)?|client[_\- ]?secret|private[_\- ]?key|"
              r"密碼|密码|口令|金鑰|密鑰|密钥|令牌)")
_RE_CRED_VALUE = re.compile(_CRED_WORD + r"\s*(?:is|was|=|:|：|是|係|为|為|->)\s*[\"'`「]?([^\s\"'`」，。,;；]{8,})",
                            re.IGNORECASE)
_RE_STRONG = [
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),            # Telegram bot token
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),                       # GitHub fine-grained PAT
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),                           # GitLab PAT
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),                              # Hugging Face
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),                             # npm
    re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}"),                         # Stripe live
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),   # JWT
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}\b"),
]
_RE_URL_CRED = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s/@]{6,})@", re.IGNORECASE)   # scheme://user:password@host
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"))
_PLACEHOLDER = re.compile(r"^(?:x+|\*+|\.+|<[^>]*>|\{[^}]*\}|\$\{?\w+\}?|your[_\-]?\w*|changeme|redacted|none|null|"
                          r"required|optional|placeholder|example\w*|env\w*)$", re.IGNORECASE)


def _secret_like(value: str) -> bool:
    """A credential value: not a placeholder / env reference, and either mixes letters with digits or symbols,
    or is a long hex / base64 run."""
    v = value.strip(".")
    if len(v) < 8 or _PLACEHOLDER.match(v) or v.startswith(("$", "~/", "/", "http")):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{24,}|[A-Za-z0-9+/_\-]{32,}={0,2}", v):
        return True
    has_alpha, has_digit = bool(re.search(r"[A-Za-z]", v)), bool(re.search(r"\d", v))
    has_sym = bool(re.search(r"[!@#%^&*+=?]", v))
    return (has_alpha and has_digit) or (has_alpha and has_sym)


def looks_secret(text: str) -> str | None:
    """Reason string if *text* appears to contain a credential, else None."""
    t = unicodedata.normalize("NFKC", (text or "").translate(_ZERO_WIDTH))   # full-width forms, zero-width splits
    for rx in _RE_STRONG:
        if rx.search(t):
            return "looks like an API key / private key"
    for m in _RE_CRED_VALUE.finditer(t):
        if _secret_like(m.group(1)):
            return "looks like a password / token value"
    for m in _RE_URL_CRED.finditer(t):
        if _secret_like(m.group(1)):
            return "looks like a URL with an embedded password"
    return None


def looks_secret_label(value: str) -> str | None:
    """Reason string if a single short label (one tag, one entity name or alias) is itself a credential, else None.
    Labels are not prose: "word = value" / "word: value" patterns are not applied (a tag "token" next to "issue-1234" or
    an entity "Refresh Token=refresh_token|RT2" is ordinary). Refused: the known key shapes (also inside the label),
    a URL with an embedded password, and a 32+ character token that mixes upper case,
    lower case and digits."""
    t = unicodedata.normalize("NFKC", (value or "").translate(_ZERO_WIDTH))
    for rx in _RE_STRONG:
        if rx.search(t):
            return "looks like an API key / private key"
    for m in _RE_URL_CRED.finditer(t):
        if _secret_like(m.group(1)):
            return "looks like a URL with an embedded password"
    for word in t.split():
        w = word.strip(".,;:()[]{}\"'`")
        if (re.fullmatch(r"[A-Za-z0-9+/_\-]{32,}={0,2}", w) and re.search(r"[A-Z]", w) and re.search(r"[a-z]", w)
                and re.search(r"\d", w)):
            return "looks like a long random token"
    return None


# What a credential looks like AFTER Hermes' egress scrub (agent/redact.py) masked it on the way to the provider:
# "sk-pro...IiHh", "hf_Zz9...OoNn", "postgres://admin:***@host", "aws_secret_access_key=***". Harmless, but a note
# holding one is useless (the value is gone) and only clutters recall, so explicit writes refuse it.
_MASK_PREFIX = r"(?:sk[-_]|rk_|hf_|gh[pousr]_|github_|glpat|npm_|xox[abprs]|AIza|AKIA|ASIA|eyJ|Bearer\s)"
_RE_MASKED = (
    re.compile(r"(?<![A-Za-z0-9])" + _MASK_PREFIX + r"[A-Za-z0-9_\-]{0,6}\.\.\.[A-Za-z0-9_\-]{3,6}(?![A-Za-z0-9])"),
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:\*{3,}@", re.IGNORECASE),
    re.compile(_CRED_WORD + r"\s*[=:：]\s*[\"']?\*{3,}", re.IGNORECASE),
    re.compile(r"\b\w*(?:secret|token|passw(?:or)?d|api[_\-]?key)\w*\s*[=:：]\s*[\"']?\*{3,}", re.IGNORECASE),
)


def looks_masked_secret(text: str) -> str | None:
    """Reason string if *text* holds a credential that was already masked upstream, else None."""
    t = unicodedata.normalize("NFKC", (text or "").translate(_ZERO_WIDTH))
    for rx in _RE_MASKED:
        if rx.search(t):
            return "contains a masked credential (the value was already redacted, e.g. sk-pro...abcd or user:***@)"
    return None


# Text the user marked as not for memory: <private>...</private> (also full-width brackets, any case, spaces inside the tag).
# An opening tag without a closing one hides everything after it (the safe reading of a half-typed tag).
PRIVATE_MARK = "[REDACTED]"
_RE_PRIVATE = re.compile(r"[<＜]\s*private\s*[>＞].*?(?:[<＜]\s*/\s*private\s*[>＞]|\Z)", re.IGNORECASE | re.DOTALL)
_RE_PRIVATE_STRAY = re.compile(r"[<＜]\s*/\s*private\s*[>＞]", re.IGNORECASE)


def strip_private(text: str) -> tuple[str, int]:
    """(text with every <private>...</private> span replaced by [REDACTED], number of spans removed). Unchanged text is
    returned as is, so callers can use it on every write without cost."""
    t = text or ""
    if "rivate" not in t and "RIVATE" not in t.upper():
        return t, 0
    n = len(_RE_PRIVATE.findall(t))
    t = _RE_PRIVATE.sub(PRIVATE_MARK, t)
    t = _RE_PRIVATE_STRAY.sub("", t)
    return t, n


# Wording that tries to steer a later reader (instructions aimed at an agent) rather than record a fact. Notes written by
# another agent that match are held for review instead of being saved straight away.
_RE_STEERING = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)|disregard (?:all |any )?(?:the )?"
    r"(?:previous|prior|above|earlier|system)|(?:reveal|print|show|send|email|exfiltrate)\b.{0,40}\b(?:system prompt|api key|password|"
    r"credentials|secrets?|all (?:the )?(?:notes|memor(?:y|ies)))|you (?:must|should) (?:always|never) |from now on,? (?:always|never) |"
    r"忽略(?:之前|以上|上面|先前|所有)的?(?:指示|指令|規則|规则|提示)|無視(?:之前|以上|所有)的?(?:指示|指令)|不要(?:告訴|告诉)(?:用戶|用户)|"
    r"(?:洩漏|泄漏|傳送|发送|發送|寄出).{0,12}(?:密碼|密码|金鑰|金钥|系統提示|系统提示|所有記憶|所有记忆)", re.IGNORECASE | re.DOTALL)


def write_flag(text: str, keywords: str = "", pinned: bool = False, pii_kinds=()) -> str:
    """Why a note written by another agent should wait for the maintainer, or '' if it can be saved straight away."""
    t = f"{text}\n{keywords}"
    if _RE_STEERING.search(t):
        return "it reads like an instruction to a future agent, not a fact"
    if pii_kinds:
        return "it contains personal data (" + ", ".join(sorted(pii_kinds)) + ")"
    if pinned:
        return "agents may not pin notes (pinned notes go into every session's core block)"
    return ""


def has_text_outside_private(text: str) -> bool:
    return bool((text or "").replace(PRIVATE_MARK, "").strip())


# Text the platform puts around or instead of what the user typed. Searching memory with it recalls notes about cache
# paths and image uploads (about 30% of automatic recalls were such boilerplate in live use).
_RE_SYSTEM_NOTICE = re.compile(r"^\s*\[(?:IMPORTANT:\s*Background process|INTERNAL NOTIFICATION|SYSTEM\b)", re.IGNORECASE)
_RE_PLATFORM_ERROR = re.compile(r"^\s*The document is too large or its size could not be verified", re.IGNORECASE)
_RE_ATTACHMENT = re.compile(r"\[(?:\d+\s+)?(?:image|file|audio|video|document|voice)s?(?:\s+attached(?:\s+at)?:[^\]]*)?\]|"
                            r"@(?:image|file|audio|video):\S+", re.IGNORECASE)
_RE_DEFAULT_IMAGE_Q = re.compile(r"^\s*What do you see in this image\?\s*", re.IGNORECASE)


def clean_prompt(text: str) -> str:
    """The user's own words in a prompt: '' for platform notices (background-process results, upload errors);
    attachment markers and the default image question removed."""
    t = text or ""
    if _RE_SYSTEM_NOTICE.match(t) or _RE_PLATFORM_ERROR.match(t):
        return ""
    t = _RE_ATTACHMENT.sub(" ", t)
    t = _RE_DEFAULT_IMAGE_Q.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


_RE_INJECTED = re.compile(r"<(?P<tag>[\w\-]*(?:memory|context)[\w\-]*)>.*?</(?P=tag)>", re.IGNORECASE | re.DOTALL)
_RE_DATA_URI = re.compile(r"!?\[[^\]\n]{0,80}\]\(\s*data:[^)]*\)|data:[\w.+\-]+/[\w.+\-]+;base64,[A-Za-z0-9+/=\s]{16,}",
                          re.IGNORECASE)


def strip_injected(text: str) -> str:
    """Remove recalled-memory blocks and data URIs; collapse the blank lines left behind."""
    t = _RE_INJECTED.sub("", text or "")
    t = _RE_DATA_URI.sub("[image]", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()
