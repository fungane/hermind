"""Shared constants and pure helpers for the hermind store (split out of the former 1,950-line store.py).

Original module description: hermind store (v0.3): SQLite notes + own unigram/bigram postings table, an entity graph, deterministic
consolidation (near-duplicate merge, newer-state supersede) and multi-signal retrieval fused with reciprocal rank
fusion (BM25, keyword/alias field, query entities, one-hop entity expansion, temporal, recency, kind/proof prior).
Pure stdlib + SQLite, no Hermes imports, so tools/ and tests/ can use it directly."""

from __future__ import annotations

# noqa: F401 -- names below are re-exported to the store mixins
import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

try:
    from .screen import has_text_outside_private, looks_secret, strip_private
except ImportError:
    from screen import has_text_outside_private, looks_secret, strip_private
try:
    from .textnorm import (KINDS, RE_CJK_CHAR, STOP_LATIN, classify_kind, core_text, normalize, numbers,
                           parse_dates, query_time_window, sim_features, slot_values, tokenize, token_weight,
                           query_token_weights, query_term_factor)
except ImportError:  # loaded as a top-level module (tools/, import_hindsight.py)
    from textnorm import (KINDS, RE_CJK_CHAR, STOP_LATIN, classify_kind, core_text, normalize, numbers,
                          parse_dates, query_time_window, sim_features, slot_values, tokenize, token_weight,
                          query_token_weights, query_term_factor)


# Ranking prior per kind: decision > tech/state > preference > chat > news.
KIND_WEIGHT = {"decision": 1.30, "tech": 1.15, "state": 1.15, "preference": 1.10, "chat": 1.00, "news": 0.90, "summary": 1.25}
# Lifetime per kind in days; None = never expires.
KIND_TTL_DAYS = {"news": 14, "chat": 90, "decision": None, "preference": None, "tech": None, "state": None}

DAY = 86400.0
FIELD_CONTENT, FIELD_KEYWORDS, FIELD_GEN, FIELD_LEARNED = 0, 1, 2, 3
FIELD_WEIGHT = {FIELD_CONTENT: 1.0, FIELD_KEYWORDS: 1.4}
# v0.4: LLM-generated aliases (tools/backfill_aliases.py) and terms learned from use are indexed as their own
# posting fields; their weights are the "gen_field" / "learned_field" entries of the search weights.
LEARNED_MAX_TERMS = 20
LEARN_WINDOW_SECONDS = 600.0
EXPANSION_WEIGHT = 0.75
K1, B = 1.2, 0.75
RECENCY_HALF_LIFE_DAYS = 120.0
SCHEMA_VERSION = 5   # 5: review, valid_from, shown_count/opened_count/fb_at (additive columns only)
# Bump when textnorm.tokenize/stem output changes: an existing database is then re-indexed once on open.
TOKENIZER_VERSION = "2"  # 2: derivational English suffixes (presentation/present) in textnorm.stem
# Skip scoring terms whose document frequency exceeds this share of notes (True: CJK unigram, False: other).
MAX_DF_FRACTION = {True: 0.08, False: 0.4}

# Reciprocal-rank-fusion weights per signal (configurable per store / per call). "k" is the RRF constant.
DEFAULT_WEIGHTS = {"k": 10.0, "bm25": 1.0, "keywords": 0.25, "entity": 0.35, "graph": 0.2, "temporal": 0.5,
                   "recency": 0.05, "prior": 0.1, "trust": 0.3, "fold": 0.9, "candidates": 200, "diversity": 0.6,
                   "cap_per_source": 50, "graph_fanout": 30, "graph_tanh": 1,
                   "hotness": 0.05, "multi_original": 1.5, "multi_k": 10.0, "multi_pool": 30,
                   "gen_field": 0.5, "learned_field": 0.3}
MAX_MULTI_QUERIES = 4
HOT_HALF_LIFE_DAYS = 30.0
ACCESS_FLUSH_SECONDS = 60.0
# Rank-space arm boost (Hindsight recall_boost): a boosted arm contributes 1/(k + rank/divisor) instead of having
# its weight multiplied (which turns RRF into a lexicographic sort once the weight exceeds the RRF spread).
BOOST_DIVISOR = {"none": 1.0, "low": 2.0, "medium": 4.0, "high": 8.0}
DEFAULT_BOOST: dict[str, str] = {}
# Arms that generate candidates (the others only rerank them); cap_per_source applies to these.
SOURCE_ARMS = ("bm25", "keywords", "entity", "graph", "temporal")
TRUST_DEFAULT, TRUST_HIDE_BELOW = 0.5, 0.2
# source_session of a note written by `hermind import`: "import:<reader>". Notes that come from Hermind's own export carry
# IMPORT_HERMIND (the Markdown reader sets it for files with id and rev; a reader of Hermind's JSON export should too).
# Every other "import:" value is "another tool": such a note stays out of the core block until it is used (see store_core).
IMPORT_PREFIX, IMPORT_HERMIND = "import:", "import:hermind"
_RE_NEGATION = re.compile(r"不|唔|冇|沒有|没有|無法|未|別|停用|取消|放棄|\b(?:no|not|never|don't|doesn't|won't|"
                          r"stop(?:ped)?|disable[ds]?|avoid)\b", re.IGNORECASE)


DB_NAME = "hermind.db"
LEGACY_DB_NAME = "decision_memory.db"   # the project's earlier name; existing installs keep using it until renamed


def default_db_file(home) -> "Path":
    """The Hermes-route database inside a Hermes home: hermind.db, unless an old decision_memory.db is there and
    hermind.db is not (an existing install keeps its file; nothing is moved or renamed implicitly)."""
    from pathlib import Path as _P
    home = _P(home)
    old, new = home / LEGACY_DB_NAME, home / DB_NAME
    return old if old.exists() and not new.exists() else new


def with_legacy_fallback(path) -> "Path":
    """A path that ends in hermind.db but does not exist, next to an old decision_memory.db that does, means the old file."""
    from pathlib import Path as _P
    p = _P(path)
    old = p.with_name(LEGACY_DB_NAME)
    return old if p.name == DB_NAME and not p.exists() and old.exists() else p


class RefusedWrite(ValueError):
    """A write refused by a screen (credential-like content, or content the user asked to forget)."""

# Consolidation thresholds.
MERGE_JACCARD = 0.6          # near-duplicate (same kind)
MERGE_JACCARD_ANY_KIND = 0.75
MERGE_CONTAINMENT = 0.85     # smaller note's features almost all inside the larger one
# Idle decay: kind -> (floor, half-life in days). Score is multiplied by floor + (1-floor) * 0.5**(idle/half-life);
# idle = time since the note was last recalled (or created). Never hides anything; decision/tech/preference are exempt.
IDLE_DECAY = {"chat": (0.3, 60.0), "news": (0.3, 30.0), "state": (0.6, 120.0)}
# A state note that is only a progress snapshot ("用戶正在排查…", "下一步…", "pending") goes stale within weeks; when
# nobody recalls it, it fades faster than a lasting state fact ("VPS has 3.8GB RAM"). Recalling it resets the clock.
PROGRESS_DECAY = (0.35, 21.0)
RE_PROGRESS = re.compile(r"正在|下一步|待驗證|待验证|待辦|待办|接下來|接下来|準備|准备|打算|\bpending\b|"
                         r"\b(?:is|are) (?:now |still )?(?:trying|working on|debugging|testing|investigating|preparing)\b",
                         re.IGNORECASE)
PENDING_WEIGHT = 0.6        # score multiplier for a note still waiting in the review queue (LLM-extracted, unconfirmed)
# Open-rate prior: how often a note, once shown, was actually opened with memory_get. A small, bounded nudge.
FB_HALF_LIFE_DAYS = 90.0     # counts fade with this half-life (decayed on every update and when read)
FB_MIN_SHOWN = 5.0           # effective shown count below which the prior does not apply at all
FB_PRIOR_STRENGTH = 5.0      # pseudo-observations pulling a note's rate towards the corpus-wide rate
FB_GAIN = 0.5                # rate difference -> score effect (0.2 above the corpus rate already reaches the cap)
FB_MAX_EFFECT = 0.10         # hard cap: the multiplier stays within 0.90 .. 1.10
STALE_PENALTY = 0.55         # score multiplier for a state note whose swap/ram figure was later changed
SUPERSEDE_JACCARD = 0.4      # same topic, conflicting quantities -> newer state supersedes older

GENERIC_ENTITIES = {normalize(x) for x in (
    "user", "users", "assistant", "the user", "用戶", "使用者", "用户", "助手", "助理", "agent", "ai", "bot", "system",
    "hermes agent (assistant)", "me", "i", "we", "you")}
_COMMON_WORDS = set(STOP_LATIN) | set((
    "user users assistant when involving note notes after before because however also next first then today new old "
    "yes no not all any some each every other more most less many much few one two three four five use uses used using "
    "run runs running check checked set setup install installed update updated add added make made get got need needs "
    "plan step steps current currently now still only just like via per vs etc ok okay please thanks error errors "
    "file files folder path data model models config server service tool tools task tasks time date day days week month "
    "year free paid local remote main default option options mode test tests result results issue issues problem "
    "fix fixed support supports app apps api key keys token tokens memory disk ram cpu gpu if it its this that these "
    "those there here they their he she his her them our your my mine we us i you an a the and or but is are was were "
    "be been being has have had do does did done can could should would will shall may might must on in at by for "
    "from to of with without into onto over under about above below up down out off again further once both few own "
    "same so than too very s t just don should now assistant's user's hermes's").split())

_MONTHS_DAYS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "mon",
                "tue", "wed", "thu", "fri", "sat", "sun"}
_RE_LATIN_TOK = re.compile(r"[0-9a-z][0-9a-z_.+\-/]*[0-9a-z]|[0-9a-z]")
_RE_CAP_SEQ = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[.\-+][A-Za-z0-9]+)*(?:\s+(?:[A-Z][A-Za-z0-9]*(?:[.\-+][A-Za-z0-9]+)*"
                         r"|\d+(?:\.\d+)+[A-Za-z]*|v?\d+(?:\.\d+)*))*)")
_RE_TECHY = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[._\-+][A-Za-z0-9]+)+\b|\b[A-Za-z]+\d[A-Za-z0-9]*\b")
_RE_IP = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_RE_PORT = re.compile(r"(?:\bport\s*|埠\s*|端口\s*|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\d{1,3}(?:\.\d{1,3}){3}):)"
                      r"(\d{2,5})\b", re.IGNORECASE)
_RE_VERSION = re.compile(r"\bv\d+(?:\.\d+){1,3}\b")
_RE_QUOTED = re.compile(r"[「『“\"`]([^「」『』“”\"`\n]{2,30})[」』”\"`]")


def _now() -> float:
    return time.time()


def _entity_key(name: str) -> str:
    n = normalize(name).strip()
    if RE_CJK_CHAR.search(n):
        return re.sub(r"\s+", "", n)
    return " ".join(_RE_LATIN_TOK.findall(n))


def _valid_entity(key: str) -> bool:
    if not key or key in GENERIC_ENTITIES or len(key) < 2 or len(key) > 60:
        return False
    if key.isdigit() and len(key) < 2:
        return False
    if not RE_CJK_CHAR.search(key) and all(w in _COMMON_WORDS or w.isdigit() for w in key.split()):
        return False
    return True


def extract_entities(text: str) -> dict[str, str]:
    """Entity candidates found by pattern (no dictionary): capitalised / technical Latin names (with a trailing
    version), IPs, ports, versions and quoted names. Returns key -> display name."""
    out: dict[str, str] = {}

    def put(name: str, numeric: bool = False) -> None:
        name = name.strip(" .,:;!?()[]{}'\"")
        key = _entity_key(name)
        if key and key not in out and (_valid_entity(key) or (numeric and key.isdigit() and len(key) >= 2)):
            out[key] = name

    raw = text or ""
    for m in _RE_CAP_SEQ.finditer(raw):
        words = m.group(1).split()
        # drop leading/trailing common words ("The VPS" -> "VPS", "User Alice" -> "Alice")
        while words and (words[0].lower() in _COMMON_WORDS or words[0].lower()[:3] in _MONTHS_DAYS):
            words = words[1:]
        while words and words[-1].lower() in _COMMON_WORDS:
            words = words[:-1]
        if not words:
            continue
        words = words[:4]
        head = words[0]
        if len(words) == 1 and head[0].isupper() and head[1:].islower() and len(head) > 1:
            # a single Title-case word opening an English sentence is usually just an English word
            before = raw[:m.start()].rstrip()
            if (not before or before[-1] in ".!?|:;。！？：；\n") and re.match(r"\s+[a-z]", raw[m.end():m.end() + 2]):
                continue
        put(" ".join(words))
        if len(words) > 1 and re.match(r"v?\d", words[-1]):  # "Sonnet 5.5" -> also "Sonnet"
            put(" ".join(w for w in words if not re.match(r"v?\d", w)))
    for rx in (_RE_TECHY, _RE_IP, _RE_VERSION):
        for m in rx.finditer(raw):
            put(m.group(0) if rx is not _RE_IP else m.group(1))
    for m in _RE_PORT.finditer(raw):
        put(m.group(1), numeric=True)
    for m in _RE_QUOTED.finditer(raw):
        q = m.group(1).strip()
        if len(q.split()) <= 4:
            put(q)
    return out


# ---------------------------------------------------------------------------------------------------------------
# synonyms.yaml: one group per line, "key: alt1|alt2|alt3" (a YAML mapping of strings; parsed without PyYAML).
# Every member of a group expands to every other member.

def load_synonyms(path: str | os.PathLike | None = None) -> list[list[str]]:
    path = Path(path) if path else Path(__file__).with_name("synonyms.yaml")
    groups: list[list[str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return groups
    for line in lines:
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        rest = rest.strip().strip("\"'")
        members = [key.strip().strip("\"'")] + [m.strip() for m in rest.split("|")]
        members = [m for m in dict.fromkeys(members) if m]
        if len(members) > 1:
            groups.append(members)
    return groups


class Synonyms:
    def __init__(self, groups: list[list[str]]):
        self.groups = groups
        self._by_term: dict[str, set[int]] = defaultdict(set)
        for i, g in enumerate(groups):
            for term in g:
                self._by_term[normalize(term)].add(i)
        self._terms = sorted(self._by_term, key=len, reverse=True)

    def expand(self, text: str) -> list[str]:
        """Alternative phrasings for every synonym-table term found in *text* (substring match, normalised)."""
        return list(dict.fromkeys(a for _, alts in self.expand_terms(text) for a in alts))

    def expand_terms(self, text: str) -> list[tuple[str, list[str]]]:
        """[(matched term, its alternatives)] for every synonym-table term found in *text*."""
        low = normalize(text)
        out = []
        for term in self._terms:
            if term in low:
                alts = []
                for gi in self._by_term[term]:
                    alts.extend(m for m in self.groups[gi] if normalize(m) != term and normalize(m) not in low)
                if alts:
                    out.append((term, list(dict.fromkeys(alts))))
        return out

    def group_of(self, term: str) -> list[str]:
        out: list[str] = []
        for gi in self._by_term.get(normalize(term), ()):
            out.extend(self.groups[gi])
        return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------------------------------------------

def _norm_for_dedup(text: str) -> str:
    return re.sub(r"\s+", "", normalize(text))


def _digest(text: str) -> str:
    return hashlib.sha1(_norm_for_dedup(text).encode()).hexdigest()


def _bigram_set(text: str) -> set[str]:
    t = _norm_for_dedup(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def jaccard(a: str, b: str) -> float:
    x, y = _bigram_set(a), _bigram_set(b)
    if not x or not y:
        return 1.0 if _norm_for_dedup(a) == _norm_for_dedup(b) else 0.0
    return len(x & y) / len(x | y)


def _set_sim(x: frozenset, y: frozenset) -> tuple[float, float]:
    """(jaccard, containment of the smaller set) for two feature sets."""
    if not x or not y:
        return 0.0, 0.0
    inter = len(x & y)
    return inter / (len(x) + len(y) - inter), inter / min(len(x), len(y))


def default_expiry(kind: str, created_at: float) -> float | None:
    ttl = KIND_TTL_DAYS.get(kind)
    return None if ttl is None else created_at + ttl * DAY


def _parse_when(value) -> float | None:
    """since/until argument: epoch number, 'YYYY-MM-DD' or any date parse_dates understands."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    ds = parse_dates(str(value))
    if not ds:
        raise ValueError(f"cannot parse date: {value!r}")
    return ds[0]



def parse_valid(value) -> float | None:
    """valid_from / valid_to argument: None or '' -> None; epoch number; ISO 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]'
    (local time unless it ends in an offset such as +08:00 or in Z for UTC). Raises ValueError for anything else."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"cannot parse date: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("/", "-")
    try:
        if len(text) == 11 and text[-1] in "Zz":    # a date with Z: midnight UTC of that day
            return _datetime.combine(_date.fromisoformat(text[:-1]), _datetime.min.time(), tzinfo=_timezone.utc).timestamp()
        if len(text) <= 10:
            return _datetime.combine(_date.fromisoformat(text), _datetime.min.time()).timestamp()
        if text[-1] in "Zz":                        # UTC; Python 3.10's fromisoformat does not read the Z itself
            text = text[:-1] + "+00:00"
        return _datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise ValueError(f"cannot parse date: {value!r} (use YYYY-MM-DD)") from None


def day_bounds(value) -> tuple[float, float]:
    """(start of that local day, start of the next local day) for an as_of argument 'YYYY-MM-DD' (or epoch/datetime)."""
    ts = parse_valid(value)
    if ts is None:
        raise ValueError("as_of is empty")
    d = _datetime.fromtimestamp(ts).date()
    return (_datetime.combine(d, _datetime.min.time()).timestamp(),
            _datetime.combine(d + _timedelta(days=1), _datetime.min.time()).timestamp())


def valid_at(row, day_end: float) -> bool:
    """Was this note valid on the local day ending at *day_end*? valid_from <= day and (valid_to is NULL or day <
    valid_to), compared by calendar day; a NULL valid_from falls back to the creation time."""
    start = row["valid_from"] if row["valid_from"] is not None else row["created_at"]
    if start >= day_end:
        return False
    return row["valid_to"] is None or row["valid_to"] >= day_end


def iso_day(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts is not None else None


def fb_decay(fb_at: float | None, now: float) -> float:
    """Factor by which open-rate counts last updated at *fb_at* have faded by *now*."""
    if fb_at is None:
        return 1.0
    return 0.5 ** (max(0.0, now - fb_at) / DAY / FB_HALF_LIFE_DAYS)


def open_rate_factor(shown: float, opened: float, fb_at: float | None, now: float, base_rate: float,
                     max_effect: float = FB_MAX_EFFECT) -> float:
    """Score multiplier from a note's open rate: 1.0 until it was shown FB_MIN_SHOWN times (after decay), then
    1 + clamp(FB_GAIN * (smoothed rate - corpus rate), -max_effect, +max_effect). The rate is smoothed towards the
    corpus rate with FB_PRIOR_STRENGTH pseudo-observations, so a few lucky opens cannot move a note."""
    d = fb_decay(fb_at, now)
    shown, opened = float(shown or 0.0) * d, min(float(opened or 0.0), float(shown or 0.0)) * d
    if shown < FB_MIN_SHOWN:
        return 1.0
    rate = (opened + FB_PRIOR_STRENGTH * base_rate) / (shown + FB_PRIOR_STRENGTH)
    return 1.0 + max(-max_effect, min(max_effect, FB_GAIN * (rate - base_rate)))


def hotness(access_count: int, age_days: float) -> float:
    """OpenViking memory_lifecycle: sigmoid(log1p(access_count)) * 0.5 ** (age_days / half_life)."""
    return (1 / (1 + math.exp(-math.log1p(max(0, access_count))))) * 0.5 ** (max(0.0, age_days) / HOT_HALF_LIFE_DAYS)


def _clean_scope(scope: str | None) -> str:
    s = re.sub(r"[^\w.\-:@]+", "-", str(scope or "default").strip()).strip("-")
    return s[:64] or "default"


def _clean_tags(tags: str | Iterable[str] | None) -> str:
    items = tags.replace(",", " ").split() if isinstance(tags, str) else [str(t) for t in (tags or ())]
    return " ".join(dict.fromkeys(normalize(t).strip() for t in items if str(t).strip()))


def _day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


_RE_FENCE_TAG = re.compile(r"<(/?\s*[\w\-]*(?:memory|context)[\w\-]*\s*/?)>", re.IGNORECASE)


def defang_fence_tags(text: str) -> str:
    """Neutralise tags that look like a recalled-memory fence (<hermind-context>, </memory-context>, ...) so a stored
    note can never close the block it is rendered inside. Only those tags are touched; other text is unchanged."""
    return _RE_FENCE_TAG.sub(lambda m: "\u2039" + m.group(1) + "\u203a", text or "")


def l0(text: str, width: int = 60) -> str:
    """One-line summary: the note without its When:/Involving: metadata, clipped to *width* characters.
    Fence-like tags are defanged (see defang_fence_tags)."""
    return defang_fence_tags(_clip(core_text(text) or text, width))


def _clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"
