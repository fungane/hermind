"""hermind — lightweight local MemoryProvider for recalling past decisions and discussions.

Pure stdlib + SQLite: no LLM, no embeddings, no network. Semantic coverage comes from the calling agent writing
alias phrasings (keywords) at memory_remember time, plus a small editable Cantonese/written-Chinese synonym table
(synonyms.yaml) expanded at query time. Config in $HERMES_HOME/config.yaml under plugins.hermind:
db_path ($HERMES_HOME/hermind.db; an existing decision_memory.db is kept), auto_capture (true), skip_sources ("cron"), prefetch_limit (4),
capture_max_chars (600), dedup_threshold (0.85), synonyms_path (bundled synonyms.yaml), search_weights (RRF
weights, e.g. "bm25=1,entity=0.35,graph=0.2"), consolidate_on_init (true).

v0.3 adds write-side screens (injected blocks, secrets, forget tombstones), trust feedback, write-time conflict check,
memory_update / memory_get / memory_feedback, tiered output, pre-compress and built-in-memory mirror hooks, scopes/tags,
access hotness and an optional LLM layer (llm: {enabled: false}). v0.2 added an entity graph, deterministic
consolidation (near-duplicate merge / newer-state supersede, never
deleting), multi-signal retrieval fused with reciprocal rank fusion, and memory_reflect / memory_related tools.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Dict, List

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # pragma: no cover - outside a Hermes runtime (tests stub it)
    from abc import ABC as MemoryProvider  # type: ignore

try:
    from agent.memory_provider import RecallStatus
except ImportError:  # pragma: no cover
    RecallStatus = None

try:
    from tools.registry import tool_error
except ImportError:  # pragma: no cover
    def tool_error(message: str) -> str:
        return json.dumps({"error": message})

try:
    from .screen import (CONTEXT_TAG, clean_prompt, has_text_outside_private, looks_masked_secret, looks_secret, strip_injected,
                         strip_private, write_flag)
    from .pii import find_pii as _find_pii
    from .store import KINDS, LEARN_WINDOW_SECONDS, PENDING_WEIGHT, DecisionStore, RefusedWrite, day_bounds, l0
    from .store_common import default_db_file
    from .textnorm import classify_kind, topical_weight
except ImportError:  # loaded as a top-level module (e.g. pytest collecting the plugin dir)
    from screen import (CONTEXT_TAG, clean_prompt, has_text_outside_private, looks_masked_secret, looks_secret, strip_injected,
                        strip_private, write_flag)
    from pii import find_pii as _find_pii
    from store import KINDS, LEARN_WINDOW_SECONDS, PENDING_WEIGHT, DecisionStore, RefusedWrite, day_bounds, l0
    from store_common import default_db_file
    from textnorm import classify_kind, topical_weight

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "$HERMES_HOME/hermind.db"   # an existing decision_memory.db is picked up instead, see _db_path
_CONFIG_KEY = "hermind"
_LEGACY_CONFIG_KEY = "decision-memory"   # the project's earlier name; read as a fallback so existing config keeps working
_DEFAULTS = {
    "db_path": _DEFAULT_DB_PATH,
    "auto_capture": True,
    "skip_sources": "cron",
    "prefetch_limit": 4,
    "capture_max_chars": 600,
    "dedup_threshold": 0.85,
    "synonyms_path": "",
    "search_weights": "",
    "consolidate_on_init": True,
    "prefetch_chars": 1200,
    "scope": "default",
    "search_boost": "",
    "precompress_capture": True,
    "mirror_builtin": True,
    "llm": {"enabled": False, "base_url": "", "model": "", "timeout": 8},
    "vectors": {"enabled": False},      # semantic search through a separate embedding worker (see vec.py)
    "open_rate": True,                  # rank notes slightly (+-10 %) by how often they are opened once shown
    "prefetch_line_chars": 60,          # characters per recalled note line (the hook for non-Hermes agents uses 200)
    "core_summary_chars": 3500,         # pinned notes + trusted preferences in the system prompt (0 = off)
    "review_queue": True,               # LLM-extracted notes start as [pending] until confirmed (memory_review)
}
L0_CHARS, L1_CHARS = 60, 200

REMEMBER_SCHEMA = {
    "name": "memory_remember",
    "description": (
        "Save a decision, preference, technical fact or news item to long-term local memory, so it can be recalled "
        "weeks later when the user asks 'what did we decide about …' (in their own everyday words, for example colloquial "
        "Cantonese, and WITHOUT the original words). Store ONE self-contained note: what was decided/discussed, the reason, and key numbers or "
        "names.\n"
        "Save only what will still matter in a later session and cannot be re-read from the code or repo: decisions with their reason, preferences, corrections the user gave you, technical facts. Skip progress snapshots and status that goes stale within days; search first, and use memory_update instead of saving a second note on the same thing.\n"
        "keywords is REQUIRED for good recall: write 5-10 alternative phrasings the user might later use to ask for "
        "it, space- or comma-separated: colloquial Cantonese (e.g. 點算 死咗 慳錢 搬家 部機), written Chinese "
        "(e.g. 怎麼辦 下架 節省 遷移 伺服器), English terms and product names (fallback, VPS, migrate), plus broader "
        "topic words. Do not repeat words already in content.\n"
        "kind: decision (agreed conclusion / chosen plan), preference (how the user likes things), tech (setup, "
        "config, fixes), state (a dated status such as 'VPS has 3.7GB RAM free' - newer states supersede older ones), "
        "news (time-sensitive, expires in 14 days), chat (anything else, expires in 90 days), or auto.\n"
        "entities: the main names involved (products, hosts, people, projects); 'name=alias1|alias2' adds aliases "
        "such as a Cantonese nickname."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The note: one self-contained statement (with the reason)."},
            "kind": {"type": "string", "enum": list(KINDS) + ["auto"], "description": "Default: decision."},
            "keywords": {"type": "string",
                         "description": "5-10 alternative phrasings (Cantonese colloquial, written Chinese, English), "
                                        "separated by spaces or commas."},
            "pinned": {"type": "boolean", "description": "Never expires and ranks slightly higher."},
            "entities": {"type": "array", "items": {"type": "string"},
                         "description": "Main names involved; 'name=alias1|alias2' to add aliases."},
            "tags": {"type": "string", "description": "Optional labels (space-separated), e.g. a project name."},
            "valid_from": {"type": "string",
                           "description": "Optional: the day (YYYY-MM-DD) the fact started to be true, if different "
                                          "from today (e.g. a plan that took effect last month)."},
            "valid_to": {"type": "string",
                         "description": "Optional: the day (YYYY-MM-DD) the fact stops being true (exclusive), e.g. "
                                        "a subscription that ends on a known date."},
        },
        "required": ["content", "keywords"],
    },
}

SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": ("Search long-term memory for past decisions, discussions, preferences and technical notes. "
                    "Ask in any wording (Cantonese, written Chinese, English); if nothing fits, retry with a synonym "
                    "or the English term. Results show #id, kind, date and a one-line summary; use detail='more' or "
                    "memory_get(id) for more text.\n"
                    "queries (optional, up to 4): the same question in other wordings, searched together and fused "
                    "(query stays the user's original wording and counts most). If the question is colloquial "
                    "Cantonese, also pass a written-Chinese rewrite, an English rewrite, and a list of likely answer "
                    "keywords, e.g. query='部機仲剩幾多記憶體', queries=['伺服器剩餘多少記憶體', "
                    "'how much RAM is left on the server', 'RAM 記憶體 剩餘 GB']."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for (the user's own wording)."},
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                        "description": "Optional rewrites of the same question: written Chinese, English, answer "
                                       "keywords. Fused with query."},
            "kind": {"type": "string", "enum": list(KINDS), "description": "Only this kind (optional)."},
            "limit": {"type": "integer", "description": "Max results (default 5, at most 20)."},
            "since": {"type": "string", "description": "Only notes dated on/after this day (YYYY-MM-DD)."},
            "until": {"type": "string", "description": "Only notes dated on/before this day (YYYY-MM-DD)."},
            "include_history": {"type": "boolean",
                                "description": "Also return merged duplicates and superseded older versions."},
            "detail": {"type": "string", "enum": ["brief", "more"],
                       "description": "brief (default): one <=60-char line per note; more: <=200 chars + keywords. "
                                      "Use memory_get(id) for the full text."},
            "tags": {"type": "string", "description": "Only notes carrying all these tags (space-separated)."},
            "as_of": {"type": "string",
                      "description": "YYYY-MM-DD: what was true on that day - notes valid then, including older "
                                     "versions that were later superseded ('what did we use back in September?')."},
        },
        "required": ["query"],
    },
}

GET_SCHEMA = {
    "name": "memory_get",
    "description": "Full text of one note (and its older versions / merged duplicates).",
    "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "Note id (#N)."}},
                   "required": ["id"]},
}

REFLECT_SCHEMA = {
    "name": "memory_reflect",
    "description": ("Gather an evidence pack about a topic so you can reason over it: the most relevant notes "
                    "grouped by entity, with dates, kind, proof_count (how often it was confirmed), the older "
                    "superseded/merged versions behind each note, and conflicting values. Use for 'what's the "
                    "current state of …', 'how did … change', or before answering a multi-part question."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Topic or question."},
            "limit": {"type": "integer", "description": "Notes to gather (default 12, at most 25)."},
            "since": {"type": "string", "description": "YYYY-MM-DD (optional)."},
            "until": {"type": "string", "description": "YYYY-MM-DD (optional)."},
            "synthesize": {"type": "boolean",
                           "description": "Also return an LLM-written dated fact digest, oldest first, with the "
                                          "newest value of changing facts marked CURRENT (only if the "
                                          "optional LLM layer is enabled; otherwise ignored)."},
        },
        "required": ["query"],
    },
}

RELATED_SCHEMA = {
    "name": "memory_related",
    "description": "List notes about one entity (product, host, person, project; names or aliases) and the "
                   "entities most often mentioned with it.",
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Entity name or alias."},
            "limit": {"type": "integer", "description": "Max notes (default 10)."},
            "mode": {"type": "string", "enum": ["recent", "timeline"],
                     "description": "recent (default): newest notes first. timeline: oldest first, including "
                                    "superseded versions, each marked current or outdated (outdated_by / "
                                    "superseded_by) - use it to see how a fact changed over time. Each note "
                                    "carries its validity window (valid_from .. valid_to)."},
            "as_of": {"type": "string", "description": "YYYY-MM-DD: only notes that were valid on that day "
                                                       "(implies timeline mode)."},
        },
        "required": ["entity"],
    },
}

FEEDBACK_SCHEMA = {
    "name": "memory_feedback",
    "description": ("Rate a recalled note: helpful=true when it was correct and useful (trust +0.1), false when it "
                    "was wrong or misleading (trust -0.2). Low-trust notes rank lower; below 0.2 they are hidden."),
    "parameters": {"type": "object",
                   "properties": {"id": {"type": "integer", "description": "Note id (#N)."},
                                  "helpful": {"type": "boolean"}},
                   "required": ["id", "helpful"]},
}

UPDATE_SCHEMA = {
    "name": "memory_update",
    "description": ("Correct or refresh an existing note instead of adding a near-duplicate (e.g. when "
                    "memory_remember reported it as similar or a possible conflict). Same id; the old text stays in "
                    "the note's history."),
    "parameters": {"type": "object",
                   "properties": {"id": {"type": "integer", "description": "Note id (#N)."},
                                  "content": {"type": "string", "description": "The full new text of the note."},
                                  "keywords": {"type": "string",
                                               "description": "Optional: replaces the alternative phrasings."}},
                   "required": ["id", "content"]},
}

FORGET_SCHEMA = {
    "name": "memory_forget",
    "description": ("Permanently delete one note by id (when the user asks to forget it, or it is wrong). Its text "
                    "is remembered as forgotten, so it will not be auto-captured again."),
    "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "Note id (#N)."}},
                   "required": ["id"]},
}

REVIEW_SCHEMA = {
    "name": "memory_review",
    "description": ("Review queue for facts the memory extracted by itself (marked [pending] in search results; they "
                    "rank lower until confirmed). action=list shows what is waiting; confirm keeps a note as a normal "
                    "one; reject hides it and stops it being captured again (restorable with action=restore). "
                    "Confirm or reject only on the user's say-so, or when you have checked the note is right or wrong."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "confirm", "reject", "restore"],
                       "description": "list (default), confirm, reject, or restore a rejected note."},
            "id": {"type": "integer", "description": "Note id (#N) for confirm / reject / restore."},
            "ids": {"type": "array", "items": {"type": "integer"}, "description": "Several note ids at once."},
            "limit": {"type": "integer", "description": "For list: max notes (default 20)."},
            "rejected": {"type": "boolean", "description": "For list: show rejected notes instead of pending ones."},
        },
    },
}

LIST_SCHEMA = {
    "name": "memory_list",
    "description": "List the most recent notes, optionally of one kind.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "limit": {"type": "integer", "description": "Max notes (default 20)."},
        },
    },
}


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        cfg = load_config_readonly()
        return (cfg_get(cfg, "plugins", _CONFIG_KEY, default={})
                or cfg_get(cfg, "plugins", _LEGACY_CONFIG_KEY, default={}) or {})
    except Exception:
        return {}


# Chinese/English one-word acknowledgements that Hermes' own trivial-prompt filter does not cover. Looking up memory
# for them only returns noise (14-day usage log: all "no result" auto lookups were of this kind).
_ACK_RE = re.compile(
    r"^(可以|好的?|得|行|做吧|做啦|修|補吧|補啦|繼續|继续|開始|開始吧|搞掂|ok|okay|yes|no|唔該|多謝|謝謝|"
    r"都?幫我(做|搞|修)吧?|就(咁|這樣|这样)|嗯|哦|係|是|對|对|冇問題|沒問題|沒问题|可以啊|好啊|好呀)"
    r"[\s!?.,。，！？~～…、]*$", re.IGNORECASE)


def is_ack(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and len(t) <= 12 and bool(_ACK_RE.match(t))


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _split_list(value) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").replace(",", " ").split()
    return {str(v).strip().lower() for v in items if str(v).strip()}


def _date(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def _parse_weights(value) -> dict:
    if isinstance(value, dict):
        return {str(k): float(v) for k, v in value.items()}
    out = {}
    for part in str(value or "").replace(";", ",").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


def _parse_boost(value) -> dict:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in str(value or "").replace(";", ",")
                                                  .split(",")) if k.strip() and v.strip()}


def _note_date(note: dict) -> str:
    return _date(note.get("event_at") or note["created_at"])


def _brief(note: dict, width: int = L0_CHARS) -> str:
    mark = " [pending]" if note.get("review") == "pending" else ""
    return f"#{note['id']} [{note['kind']} {_note_date(note)}]{mark} {l0(note['content'], width)}"


def _tiered(note: dict, detail: str) -> dict:
    """L0 (default): id, kind, date, <=60-char line. L1 ('more'): <=200 chars plus keywords/proof/status."""
    pending = note.get("review") == "pending"
    if detail != "more":
        out = {"id": note["id"], "kind": note["kind"], "date": _note_date(note),
               "text": ("[pending] " if pending else "") + l0(note["content"], L0_CHARS)}
        if pending:
            out["pending"] = True
        return out
    out = _public(note)
    out["content"] = l0(note["content"], L1_CHARS)
    out["date"] = _note_date(note)
    if len(" ".join(note["content"].split())) > L1_CHARS:
        out["truncated"] = True
    return out


def _public(note: dict) -> dict:
    out = {"id": note["id"], "kind": note["kind"], "date": _date(note["created_at"]), "content": note["content"]}
    if note.get("keywords"):
        out["keywords"] = note["keywords"]
    if note.get("expires_at"):
        out["expires"] = _date(note["expires_at"])
    if note.get("valid_from") is not None:
        out["valid_from"] = _date(note["valid_from"])
    if note.get("valid_to") is not None:
        out["valid_to"] = _date(note["valid_to"])
    if note.get("pinned"):
        out["pinned"] = True
    if (note.get("proof_count") or 1) > 1:
        out["proof_count"] = note["proof_count"]
    if note.get("status") and note["status"] != "active":
        out["status"] = note["status"]
        out["canonical_id"] = note.get("canonical_id")
    if note.get("trust") is not None and note["trust"] != 0.5:
        out["trust"] = round(note["trust"], 2)
    if note.get("outdated_by"):
        out["outdated_by"] = note["outdated_by"]
    if note.get("candidate"):
        out["candidate"] = True
    if note.get("review") == "pending":
        out["pending"] = True
    if note.get("tags"):
        out["tags"] = note["tags"]
    if note.get("scope") and note["scope"] != "default":
        out["scope"] = note["scope"]
    return out


class HermindProvider(MemoryProvider):
    """Local decision/discussion memory with Cantonese-aware lexical recall."""

    _CONTEXT_OPEN, _CONTEXT_CLOSE = f"<{CONTEXT_TAG}>", f"</{CONTEXT_TAG}>"

    def __init__(self, config: dict | None = None):
        self._config = {**_DEFAULTS, **(config or {})}
        self._store: DecisionStore | None = None
        self._session_id = ""
        self._writable = True
        self._skip = False
        self._platform = ""
        self._lock = threading.RLock()
        self._last_count: int | None = None
        self._llm = None
        self._vec = None
        self._pool = None          # single background worker for LLM fact extraction (never blocks the reply)
        # learning from use: last 5 searches per session (in memory only): (time, original query, result ids)
        self._recent: "OrderedDict[str, deque]" = OrderedDict()
        self._listed: "OrderedDict[str, deque]" = OrderedDict()   # per session: (time, usage_log id, {note ids}) of recent listings
        # open-rate feedback: per session, the notes listed so far -> already opened? (bounded, in memory only)
        self._shown: "OrderedDict[str, OrderedDict[int, bool]]" = OrderedDict()
        self._clock = time.time

    @property
    def name(self) -> str:
        return "hermind"

    def is_available(self) -> bool:
        return True  # stdlib + sqlite only

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "db_path", "description": "SQLite file (profile-scoped by default)", "default": _DEFAULT_DB_PATH},
            {"key": "auto_capture", "description": "Store each turn as a short chat note (expires in 90 days)",
             "type": "boolean", "default": True},
            {"key": "skip_sources", "description": "Session sources never auto-captured (comma-separated)",
             "default": "cron"},
            {"key": "prefetch_limit", "description": "Notes injected per turn", "type": "integer", "default": 4,
             "minimum": 0, "maximum": 10},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        # Same path as the reference plugin: canonical writer, merged, scoped to the profile being configured.
        from hermes_cli.config import save_config
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        values = dict(values)
        db_path = values.get("db_path")
        if isinstance(db_path, str) and Path(db_path).expanduser() in (Path(hermes_home) / "hermind.db", Path(hermes_home) / "decision_memory.db"):
            values["db_path"] = _DEFAULT_DB_PATH
        token = set_hermes_home_override(hermes_home)
        try:
            save_config({"plugins": {_CONFIG_KEY: values}}, merge_existing=True)
        finally:
            reset_hermes_home_override(token)

    def _make_llm(self):
        """Optional LLM layer: only imported/constructed when llm.enabled is true (zero cost when off)."""
        cfg = self._config.get("llm") or {}
        if not isinstance(cfg, dict) or not _truthy(cfg.get("enabled", False)):
            return None
        try:
            from .llm import LLMClient
        except ImportError:  # top-level module load
            from llm import LLMClient
        return LLMClient(cfg)

    def _make_vec(self, db_path: str):
        cfg = self._config.get("vectors") or {}
        if not isinstance(cfg, dict) or not _truthy(cfg.get("enabled", False)):
            return None
        try:
            from .vec import VectorIndex
        except ImportError:
            from vec import VectorIndex
        return VectorIndex(cfg, db_path)

    def _vector_fuse(self, queries: list, notes: list, limit: int, filters: dict, *, index: bool = True,
                     timeout: float | None = None) -> list:
        """Add the embedding worker's nearest notes to the lexical results and fuse both rankings (RRF, k=60).
        index=False skips embedding new notes first (the automatic recall must stay fast)."""
        try:
            if index:
                self._vec.index_missing(self._store, 32)
            lists = [[n["id"] for n in notes]]
            by = {n["id"]: n for n in notes}
            for q in queries[:3]:
                hits = self._vec.search(q, timeout=timeout)
                if hits is None:
                    break
                lists.append([i for i, _ in hits])
            if len(lists) == 1:
                return notes
            extra = [i for L in lists[1:] for i in L if i not in by]
            if extra:
                keep = {k: filters[k] for k in ("kind", "since", "until", "tags", "as_of") if k in filters}
                for n in self._store.notes_by_ids(list(dict.fromkeys(extra)), **keep):
                    by[n["id"]] = n
            score: dict = {}
            for L in lists:
                for rank, i in enumerate(L):
                    if i in by:
                        score[i] = score.get(i, 0.0) + 1.0 / (60 + rank + 1)
            for i in score:   # the review queue's down-weight must also hold for vector-only hits
                if by[i].get("review") == "pending":
                    score[i] *= PENDING_WEIGHT
            return [by[i] for i, _ in sorted(score.items(), key=lambda x: -x[1])]
        except Exception:
            return notes

    def _scope(self, hermes_home: str, kwargs: dict) -> str:
        """config scope with {profile} (HERMES_HOME directory name / agent_identity) and {platform} filled in."""
        raw = str(self._config.get("scope") or "default")
        profile = str(kwargs.get("agent_identity") or kwargs.get("profile") or Path(hermes_home).name or "default")
        return raw.replace("{profile}", profile).replace("{platform}", str(kwargs.get("platform") or "cli"))

    def _db_path(self, hermes_home: str) -> str:
        p = str(self._config.get("db_path") or _DEFAULT_DB_PATH)
        if p == _DEFAULT_DB_PATH:
            return str(default_db_file(hermes_home))
        p = p.replace("${HERMES_HOME}", hermes_home).replace("$HERMES_HOME", hermes_home)
        return str(Path(p).expanduser())

    def backup_paths(self) -> list[str]:
        return []  # the default DB lives inside HERMES_HOME and is backed up with it

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def _is_skipped_source(self, session_id: str = "", **kwargs) -> bool:
        skip = _split_list(self._config.get("skip_sources", "cron"))
        if not skip:
            return False
        values = [str(kwargs.get(k) or "") for k in ("agent_context", "platform", "source", "session_source",
                                                      "trigger")]
        values.append(session_id or "")
        for v in values:
            low = v.lower()
            if not low:
                continue
            if low in skip or any(low.startswith(s + "_") or low.startswith(s + ":") or low.startswith(s + "-")
                                  for s in skip):
                return True
        if "cron" in skip and (kwargs.get("is_cron") or kwargs.get("scheduled") or kwargs.get("cron_job_id")):
            return True
        return False

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home")
        if not hermes_home:
            try:
                from hermes_constants import get_hermes_home
                hermes_home = str(get_hermes_home())
            except Exception:
                hermes_home = str(Path.home() / ".hermes")
        if self._store is not None:
            self.shutdown()
        self._session_id = session_id or ""
        self._platform = str(kwargs.get("platform") or "")
        context = str(kwargs.get("agent_context") or "primary")
        self._writable = context == "primary"
        self._skip = self._is_skipped_source(session_id, **kwargs)
        weights = _parse_weights(self._config.get("search_weights"))
        if not _truthy(self._config.get("open_rate", True)):
            weights["openrate"] = 0.0
        self._store = DecisionStore(self._db_path(str(hermes_home)),
                                    synonyms_path=self._config.get("synonyms_path") or None,
                                    weights=weights,
                                    boost=_parse_boost(self._config.get("search_boost")),
                                    scope=self._scope(str(hermes_home), kwargs), track_access=True)
        self._llm = self._make_llm()
        self._vec = self._make_vec(self._db_path(str(hermes_home)))
        try:
            self._store.maintenance(consolidate=_truthy(self._config.get("consolidate_on_init", True))
                                    and self._writable)
        except Exception as exc:  # pragma: no cover
            logger.debug("hermind maintenance failed: %s", exc)

    def shutdown(self) -> None:
        if self._vec is not None:
            self._vec.close()
        if self._pool is not None:             # let queued extractions finish (bounded) before closing the database
            self._pool.shutdown(wait=True, cancel_futures=False)
            self._pool = None
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id
        self._skip = self._is_skipped_source(self._session_id, platform=self._platform, **kwargs)

    def _core_block(self) -> str:
        """Deterministic core summary (pinned notes + most trusted preferences) for the system prompt; '' if none."""
        if self._store is None:
            return ""
        try:
            chars = int(self._config.get("core_summary_chars", 3500) or 0)
            with self._lock:
                body = self._store.core_summary(chars) if chars else ""
        except Exception as exc:   # the system prompt must never fail because of this block
            logger.debug("hermind core summary failed: %s", exc)
            return ""
        return f"{self._CONTEXT_OPEN}\n{body}\n{self._CONTEXT_CLOSE}" if body else ""

    def system_prompt_block(self) -> str:
        base = self._base_prompt_block()
        core = self._core_block()
        return f"{base}\n\n{core}" if core else base

    def _base_prompt_block(self) -> str:
        langs = str(self._config.get("phrasing_languages", "") or "").strip() or \
            "the languages the user actually speaks (e.g. Cantonese colloquial, written Chinese, English)"
        return ("Long-term memory (hermind): when a decision, preference or important technical fact is "
                "settled (not progress or status that will be stale in days), call memory_remember with 5-10 alternative phrasings in keywords, written in " + langs +
                ", i.e. how the user might ask for it later WITHOUT the original words. Use memory_search when the "
                "user asks what was decided or discussed before; if the question is casual, also pass 1-2 rewrites "
                "in queries (a formal wording and the likely answer keywords). Results are short summaries: pick "
                "the relevant ones yourself and open them with memory_get(id).")

    # -- recall -------------------------------------------------------------------------------------------------

    def maintenance(self) -> int:
        return self._store.maintenance() if self._store else 0

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_count = 0
        query = strip_private(clean_prompt(query))[0].replace("[REDACTED]", " ")   # notices, markers, <private> spans: not searched
        query = " ".join(query.split())
        if self._store is None or not query:
            return ""
        try:
            from agent.memory_provider import is_trivial_prompt
            if is_trivial_prompt(query):
                return ""
        except ImportError:
            pass
        if is_ack(query):   # recall only; capture still keeps "OK" answers, which are often approvals of a decision
            return ""
        limit = int(self._config.get("prefetch_limit", 4) or 0)
        if limit <= 0:
            return ""
        # A prompt that is only filler / generic words has nothing to look up; recalling would add noise.
        if topical_weight(query) < float(self._config.get("prefetch_min_weight", 1.0) or 0.0):
            return ""
        n = max(3, min(limit, 5))
        use_vec = (self._vec is not None and self._vec.available()
                   and _truthy(self._config.get("prefetch_vectors", True)))
        with self._lock:
            notes = self._store.search(query, limit=10 if use_vec else n)
        if use_vec:
            # Lexical recall alone ranks chatty prompts by their generic words ("排查 問題"); the embedding worker
            # (~1 s cold, ~20 ms warm) pulls the topical notes back. Any failure or timeout keeps the lexical list.
            notes = self._vector_fuse([query], notes, n, {}, index=False,
                                      timeout=float(self._config.get("prefetch_vector_timeout", 3.0) or 3.0))
        notes = notes[:n]
        self._log_listing("search", [n["id"] for n in notes], source="auto", query=query, n_results=len(notes))
        budget = int(self._config.get("prefetch_chars", 1200) or 1200)
        lines, used = [], 0
        width = max(40, min(400, int(self._config.get("prefetch_line_chars", L0_CHARS) or L0_CHARS)))
        for n in notes:  # one short line per note (L0 by default), within the character budget
            line = _brief(n, width)
            if lines and used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        with self._lock:
            self._track_shown([n["id"] for n in notes[:len(lines)]])
        self._last_count = len(lines)
        return (f"{self._CONTEXT_OPEN}\nRelated past notes (hermind; memory_get(id) for full text):\n"
                + "\n".join(lines) + f"\n{self._CONTEXT_CLOSE}")

    def recall_status(self):
        if RecallStatus is None or not self._last_count:
            return None
        return RecallStatus(provider_label="hermind", count=self._last_count)

    # -- capture ------------------------------------------------------------------------------------------------

    def _capturing(self) -> bool:
        return (self._store is not None and self._writable and not self._skip
                and _truthy(self._config.get("auto_capture", True)))

    def _capture(self, user_content: str, assistant_content: str, session_id: str = "") -> int | None:
        # recalled memory (ours or another provider's) and inline images must not be stored as new chat notes
        user_content, assistant_content = strip_injected(user_content), strip_injected(assistant_content)
        user_content, assistant_content = strip_private(user_content)[0], strip_private(assistant_content)[0]   # <private> spans
        if not user_content or not has_text_outside_private(user_content):
            return None
        try:
            from agent.memory_provider import is_trivial_prompt
            if is_trivial_prompt(user_content):
                return None
        except ImportError:
            pass
        cap = int(self._config.get("capture_max_chars", 600) or 600)
        u = user_content[: cap // 3]
        a = assistant_content[: cap - len(u)]
        chunk = f"User: {u}\nAssistant: {a}" if a else f"User: {u}"
        # the stored chunk is truncated, but the LLM extractor would receive the full turn: screen all of it, so a
        # secret pasted late in a long message is never sent to the provider
        if looks_secret(chunk) or looks_secret(user_content) or looks_secret(assistant_content):
            return None
        sid = session_id or self._session_id
        if self._llm is not None and getattr(self._llm, "capture_extract", False):
            # LLM extraction runs in the background; on any failure the raw chunk is stored instead (old behaviour)
            key = hash((user_content[:300], assistant_content[:300]))
            if key == getattr(self, "_last_extract_key", None):      # on_session_end repeats the last turn
                return None
            self._last_extract_key = key
            if self._pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dm-extract")
            self._pool.submit(self._extract_and_store, user_content, assistant_content, chunk, sid)
            return None
        return self._store_raw(chunk, sid)

    def _bg_index(self) -> None:
        """Embed new notes in the background (never blocks the reply); a no-op when vectors are off or unavailable."""
        if self._vec is None or not self._vec.available():
            return
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dm-extract")
        self._pool.submit(lambda: self._vec.index_missing(self._store, 32) if self._store is not None else 0)

    def _store_raw(self, chunk: str, sid: str) -> int | None:
        with self._lock:
            if self._store is None:
                return None
            try:
                nid, created = self._store.add(chunk, kind="chat", source_session=sid,
                                               dedup_threshold=float(self._config.get("dedup_threshold", 0.85)))
            except RefusedWrite:  # forgotten earlier
                return None
        return nid if created else None

    def _extract_and_store(self, user: str, assistant: str, chunk: str, sid: str) -> None:
        try:
            facts = self._llm.extract_facts(strip_injected(user), strip_injected(assistant))
        except Exception:
            facts = None
        u = getattr(self._llm, "last_usage", {}) or {}
        with self._lock:
            if self._store is None:
                return
            self._store.log_event("extract", source="llm", query=user[:60], n_queries=int(u.get("prompt_tokens") or 0),
                                  n_results=-1 if facts is None else len(facts))
        if facts is None:                      # model failed -> keep the raw turn so nothing is lost
            self._store_raw(chunk, sid)
            return
        review = "pending" if _truthy(self._config.get("review_queue", True)) else "active"
        for f in facts:                        # [] -> the turn had nothing durable: store nothing
            if looks_secret(f["text"]) or looks_secret(" ".join(f["aliases"])):
                continue
            kind = f["kind"] if f["kind"] in KINDS and f["kind"] not in ("summary", "news") else "chat"
            try:
                if self._merge_into_twin(f["text"], " ".join(f["aliases"])):
                    continue
                with self._lock:
                    if self._store is not None:
                        self._store.add(f["text"], kind=kind, keywords=" ".join(f["aliases"]), source_session=sid,
                                        dedup_threshold=float(self._config.get("dedup_threshold", 0.85)),
                                        review=review)
            except (RefusedWrite, ValueError):
                continue
        self._bg_index()

    def _merge_into_twin(self, text: str, aliases: str) -> bool:
        """An extracted fact that restates an existing note (same wording to >= extract_merge_similarity) refreshes
        that note instead of adding a near-copy: the newer text replaces it (the old text stays in its history) unless
        the new one is much shorter, and proof_count goes up. Pinned and consolidated (locked) notes are never
        rewritten. True when merged."""
        threshold = float(self._config.get("extract_merge_similarity", 0.5) or 0)
        if threshold <= 0:
            return False
        with self._lock:
            if self._store is None:
                return False
            sim = [x for x in self._store.write_check(text)["similar"] if x["similarity"] >= threshold]
            if not sim:
                return False
            twin = max(sim, key=lambda x: x["similarity"])
            row = self._store.conn.execute("SELECT content, keywords, pinned, locked FROM notes WHERE id=?",
                                           (twin["id"],)).fetchone()
            if row is None or row["pinned"] or row["locked"]:
                return False
            merged_kw = " ".join(dict.fromkeys((row["keywords"] + " " + aliases).split()))
            if len(text) >= 0.8 * len(row["content"]):
                self._store.update(twin["id"], text, merged_kw)
            else:
                self._store.update_keywords(twin["id"], merged_kw)
            with self._store.conn:
                self._store.conn.execute("UPDATE notes SET proof_count=proof_count+1 WHERE id=?", (twin["id"],))
            return True

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages=None,
                  turn_author: dict | None = None) -> None:
        if not self._capturing():
            return
        if session_id and self._is_skipped_source(session_id):
            return
        if turn_author and turn_author.get("is_bot"):
            return
        try:
            self._capture(user_content, assistant_content, session_id)
        except Exception as exc:
            logger.debug("hermind sync_turn failed: %s", exc)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Capture the final user/assistant exchange if sync_turn has not already (dedup makes this idempotent)."""
        if not self._capturing() or not messages:
            return
        user = assistant = ""
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            if msg.get("role") == "assistant" and not assistant and not user:
                assistant = content
            elif msg.get("role") == "user":
                user = content
                break
        try:
            self._capture(user, assistant)
        except Exception as exc:
            logger.debug("hermind on_session_end failed: %s", exc)

    # -- compression / built-in memory mirror -------------------------------------------------------------------

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Rule-based pickup of decision / tech / state sentences from the messages about to be compressed. They are
        stored as kind=chat *candidate* notes (expire like chat, rank slightly lower) and listed in the returned
        string so the compressor keeps them in the summary."""
        if not messages or not _truthy(self._config.get("precompress_capture", True)):
            return ""
        found = None
        if self._llm is not None:
            transcript = "\n".join(f"{m.get('role')}: {strip_injected(m['content'])}" for m in messages
                                   if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                                   and not looks_secret(m["content"]))
            found = self._llm.extract_candidates(transcript)
            found = [t for t in (found or []) if not looks_secret(t)] or None
        if found is None:
            found = pick_candidates(messages)
        if not found:
            return ""
        saved = []
        if self._capturing():
            for text in found:
                try:
                    with self._lock:
                        nid, created = self._store.add(text, kind="chat", source_session=self._session_id,
                                                       candidate=True, dedup_threshold=0.8)
                    if created:
                        saved.append(nid)
                except (RefusedWrite, ValueError):
                    continue
        lines = "\n".join(f"- {_short(t, 160)}" for t in found)
        return f"Decisions/facts noted before compression (hermind saved {len(saved)}):\n{lines}"

    def on_memory_write(self, action: str, target: str, content: str, metadata: Dict[str, Any] | None = None) -> None:
        """Mirror built-in MEMORY.md / USER.md writes as pinned notes (user -> preference, memory -> state)."""
        if self._store is None or not self._writable or not _truthy(self._config.get("mirror_builtin", True)):
            return
        metadata = metadata or {}
        src = f"builtin:{target or 'memory'}"
        kind = "preference" if (target or "").lower() == "user" else "state"
        content = strip_injected(content or "")
        prev = strip_injected(str(metadata.get("previous_content") or metadata.get("old_text") or ""))
        try:
            with self._lock:
                old = self._store.find_by_content(prev, source_session=src) if prev else None
                if action == "remove":
                    if old:
                        self._store.forget(old)
                    return
                if not content or looks_secret(content):
                    return
                if action == "replace" and old:
                    self._store.update(old, content)
                    return
                self._store.add(content, kind=kind, source_session=src, pinned=True)
        except Exception as exc:
            logger.debug("hermind on_memory_write failed: %s", exc)

    # -- tools --------------------------------------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [REMEMBER_SCHEMA, SEARCH_SCHEMA, GET_SCHEMA, REFLECT_SCHEMA, RELATED_SCHEMA, UPDATE_SCHEMA,
                FEEDBACK_SCHEMA, FORGET_SCHEMA, LIST_SCHEMA, REVIEW_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        handlers = {"memory_remember": self._remember, "memory_search": self._search,
                    "memory_forget": self._forget, "memory_list": self._list, "memory_reflect": self._reflect,
                    "memory_related": self._related, "memory_update": self._update,
                    "memory_feedback": self._feedback, "memory_get": self._get,
                    "memory_review": self._review}
        if tool_name not in handlers:
            return tool_error(f"Unknown tool: {tool_name}")
        if self._store is None:
            return tool_error("hermind is not initialised")
        if (tool_name in ("memory_remember", "memory_forget", "memory_update", "memory_feedback")
                or (tool_name == "memory_review" and str((args or {}).get("action") or "list").lower() != "list")) \
                and not self._writable:
            return tool_error("hermind is read-only in this context (subagent/cron/flush)")
        try:
            with self._lock:
                return handlers[tool_name](args or {})
        except RefusedWrite as exc:
            return json.dumps({"error": str(exc), "refused": True})
        except (KeyError, ValueError, TypeError) as exc:
            return tool_error(str(exc))
        except (sqlite3.Error, OverflowError) as exc:   # e.g. an argument of the wrong type reaching SQL: never raise into the agent loop
            logger.warning("hermind %s failed: %s", tool_name, type(exc).__name__)
            return tool_error(f"{tool_name} failed ({type(exc).__name__}); check the argument types")

    def _remember(self, a: dict) -> str:
        content = str(a.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        keywords = a.get("keywords") or ""
        if isinstance(keywords, (list, tuple)):
            keywords = " ".join(str(k) for k in keywords)
        keywords = str(keywords).replace(",", " ").replace("，", " ").replace("、", " ")
        masked = looks_masked_secret(content) or looks_masked_secret(keywords)
        if masked:
            return json.dumps({"error": f"refused: {masked}; store a description without it", "refused": True},
                              ensure_ascii=False)
        ents = a.get("entities") or []
        if isinstance(ents, str):
            ents = [e for e in ents.replace("，", ",").split(",")]
        ents = [str(e).strip() for e in ents if str(e).strip()]
        if not ents and self._llm is not None:
            ents = self._llm.extract_entities(content) or []
        check = self._store.write_check(content, ents)
        pinned = _truthy(a.get("pinned", False))
        hold = self._hold_reason(content, keywords, pinned)
        if hold and self._config.get("remember_review") == "auto":
            pinned = False                                   # an agent's note never enters the core block unreviewed
        nid, created = self._store.add(content, kind=str(a.get("kind") or "decision"), keywords=keywords,
                                       source_session=self._session_id, pinned=pinned,
                                       entities=ents, tags=str(a.get("tags") or ""),
                                       review="pending" if hold else "active",
                                       valid_from=a.get("valid_from") or None, valid_to=a.get("valid_to") or None)
        if not created and keywords.strip():
            existing = self._store.get(nid)
            merged = " ".join(dict.fromkeys((existing["keywords"] + " " + keywords).split()))
            self._store.update_keywords(nid, merged)
        res = {"id": nid, "created": created}
        if created and hold:
            res["review"] = (f"held for the maintainer to confirm ({hold}); it is searchable but not trusted memory yet"
                             if hold != "all" else "held for the maintainer to confirm; it is searchable but not trusted memory yet")
        self._bg_index()
        for key in ("similar", "possible_conflicts"):
            items = [x for x in check[key] if x["id"] != nid]
            if items:
                res[key] = items
        if res.get("similar") or res.get("possible_conflicts"):
            res["note"] = ("Existing notes overlap: if the new note corrects one of them, call memory_update(id, "
                           "content) on the old note and memory_forget the new one.")
        if len(keywords.split()) < 5:
            res["hint"] = "Add 5-10 alternative phrasings in keywords (Cantonese, written Chinese, English)."
        return json.dumps(res, ensure_ascii=False)

    def _search(self, a: dict) -> str:
        query = str(a.get("query") or "").strip()
        extra = a.get("queries") or []
        if isinstance(extra, str):
            extra = [extra]
        extra = [str(q).strip() for q in extra if isinstance(q, (str, int, float)) and str(q).strip()]
        if not query and extra:
            query, extra = extra[0], extra[1:]
        if not query:
            return tool_error("query is required")
        limit = max(1, min(int(a.get("limit") or 5), 20))
        filters = dict(kind=a.get("kind") or None, since=a.get("since") or None, until=a.get("until") or None,
                       include_history=_truthy(a.get("include_history", False)), tags=str(a.get("tags") or ""),
                       as_of=a.get("as_of") or None)
        if filters["as_of"]:
            day_bounds(filters["as_of"])        # reject a malformed date up front (ValueError -> tool error)
        rr = self._llm if (self._llm is not None and getattr(self._llm, "rerank_on", False)) else None
        fetch = max(limit, self._llm.rerank_candidates) if rr else limit
        if self._vec is not None:
            fetch = max(fetch, 30)
        if extra:
            # the original wording first; at most 4 queries in total (search_multi dedupes and caps)
            notes = self._store.search_multi([query] + extra[:4], limit=fetch, **filters)
        else:
            notes = self._store.search(query, limit=fetch, **filters)
        if self._vec is not None and self._vec.available():
            notes = self._vector_fuse([query] + extra[:2], notes, limit, filters)
        if rr and len(notes) > 1:
            notes = self._apply_rerank(rr, query, notes[:rr.rerank_candidates], limit)
        notes = notes[:limit]
        self._remember_search([query] + extra[:3], notes)
        self._track_shown([n["id"] for n in notes])
        self._log_listing("search", [n["id"] for n in notes], source="tool", query=query,
                          n_queries=1 + len(extra[:4]), n_results=len(notes))
        detail = str(a.get("detail") or "brief").lower()
        out = {"results": [_tiered(n, detail) for n in notes], "count": len(notes)}
        if not notes:
            out["hint"] = ("Nothing matched. Do not repeat the same query; retry at most once with different wording "
                           "(synonyms, English term via `queries`), otherwise answer without memory.")
        return json.dumps(out, ensure_ascii=False)

    def _apply_rerank(self, rr, query: str, notes: list, limit: int) -> list:
        """Optional LLM rerank: notes the model picks go first (in its order), the rest keep the lexical order.
        Any failure or an empty pick leaves the lexical order untouched."""
        t0 = time.time()
        ids = None
        try:
            ids = rr.rerank(query, notes)
        except Exception:
            ids = None
        u = getattr(rr, "last_usage", {}) or {}
        self._store.log_event("rerank", source="llm", query=query, n_queries=int(u.get("prompt_tokens") or 0),
                              n_results=len(ids) if ids else 0)
        if not ids:
            return notes
        by = {n["id"]: n for n in notes}
        return [by[i] for i in ids] + [n for n in notes if n["id"] not in set(ids)]

    def _track_shown(self, ids: list) -> None:
        """Count each note once per session as 'shown' (search results and automatic recall lines)."""
        if (self._store is None or not self._writable or self._skip
                or not _truthy(self._config.get("open_rate", True))):
            return
        sid = self._session_id or "-"
        seen = self._shown.pop(sid, None) or OrderedDict()
        fresh = [i for i in dict.fromkeys(ids) if i not in seen]
        for i in fresh:
            seen[i] = False
        while len(seen) > 300:
            seen.popitem(last=False)
        self._shown[sid] = seen
        while len(self._shown) > 32:
            self._shown.popitem(last=False)
        if fresh:
            self._store.record_shown(fresh, now=self._clock())

    def _track_opened(self, nid: int) -> None:
        """memory_get of a note an earlier search/recall in this session listed: count it as opened (once)."""
        if self._store is None or not self._writable or self._skip \
                or not _truthy(self._config.get("open_rate", True)):
            return
        seen = self._shown.get(self._session_id or "-")
        if seen is not None and seen.get(nid) is False:
            seen[nid] = True
            self._store.record_opened(nid, now=self._clock())

    def _log_listing(self, kind: str, ids: list, **kw) -> None:
        """usage_log row for a search / automatic recall plus the ids it listed; remembered per session so that a later
        memory_get / feedback of one of those notes can point back to it (no text is stored beyond the existing 200-char query)."""
        eid = self._store.log_event(kind, shown=ids, **kw)
        if eid is None or not ids:
            return
        sid = self._session_id or "-"
        ring = self._listed.pop(sid, None) or deque(maxlen=8)
        ring.append((self._clock(), eid, frozenset(ids)))
        self._listed[sid] = ring
        while len(self._listed) > 32:
            self._listed.popitem(last=False)

    def _listing_for(self, nid: int) -> int | None:
        """Id of this session's newest listing (within 30 minutes) that contained note nid, else None."""
        now = self._clock()
        for t, eid, ids in reversed(self._listed.get(self._session_id or "-") or ()):
            if nid in ids and now - t <= 1800:
                return eid
        return None

    def _remember_search(self, queries: list, notes: list) -> None:
        if not queries or not queries[0]:
            return
        sid = self._session_id or "-"
        ring = self._recent.pop(sid, None) or deque(maxlen=5)
        ring.append((self._clock(), queries[0], [n["id"] for n in notes]))
        self._recent[sid] = ring
        while len(self._recent) > 32:  # bounded: oldest sessions dropped
            self._recent.popitem(last=False)

    def _recent_searches(self) -> list:
        """This session's searches within the learning window, newest first."""
        now = self._clock()
        ring = self._recent.get(self._session_id or "-") or ()
        return [e for e in reversed(ring) if now - e[0] <= LEARN_WINDOW_SECONDS]

    def _learn_from_use(self, nid: int, helpful: bool = True) -> None:
        """memory_get(id) / helpful feedback shortly after a search that did not rank the note first: the search's
        query terms become pending learned terms of the note (indexed after a 2nd independent confirmation).
        Unhelpful feedback removes the terms learned from the recent searches."""
        if not self._writable:
            return
        recent = self._recent_searches()
        if not recent:
            return
        try:
            if not helpful:
                for _, query, _ in recent:
                    self._store.unlearn(nid, query)
                return
            _, query, ids = recent[0]
            if ids and ids[0] == nid:
                return
            source = f"{self._session_id or '-'}|{time.strftime('%Y-%m-%d', time.localtime(self._clock()))}"
            self._store.learn(nid, query, source, now=self._clock())
        except Exception as exc:  # never let learning break a tool call
            logger.debug("hermind learning failed: %s", exc)

    def _get(self, a: dict) -> str:
        nid = int(a["id"])
        note = self._store.get(nid)
        if note is None:
            return tool_error(f"no note #{nid}. Do not guess ids; find it with memory_search first.")
        self._learn_from_use(nid)
        self._track_opened(nid)
        self._store.log_event("get", source="tool", note_id=nid, search_id=self._listing_for(nid))
        out = _public(note)
        hist = self._store.history(nid)
        if hist:
            out["history"] = [{"id": h["id"], "rel": h["rel"], "date": _note_date(h), "content": h["content"],
                               **({"valid_to": _date(h["valid_to"])} if h.get("valid_to") is not None else {})}
                              for h in hist[:10]]
        return json.dumps(out, ensure_ascii=False)

    def _reflect(self, a: dict) -> str:
        query = str(a.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        limit = max(1, min(int(a.get("limit") or 12), 25))
        pack = self._store.reflect(query, limit=limit, since=a.get("since") or None, until=a.get("until") or None)
        if _truthy(a.get("synthesize", False)) and self._llm is not None:
            answer = self._llm.synthesize(query, pack)
            if answer:
                pack["synthesis"] = answer
        return json.dumps(pack, ensure_ascii=False)

    def _related(self, a: dict) -> str:
        entity = str(a.get("entity") or "").strip()
        if not entity:
            return tool_error("entity is required")
        as_of = a.get("as_of") or None
        timeline = str(a.get("mode") or "").lower() == "timeline" or bool(as_of)
        res = self._store.related(entity, limit=max(1, min(int(a.get("limit") or 10), 50)), timeline=timeline,
                                  as_of=as_of)
        raw = {n["id"]: n for n in res["notes"]}
        res["notes"] = [_public(n) for n in res["notes"]]
        if timeline:
            for n in res["notes"]:
                src = raw[n["id"]]       # validity window; a missing valid_from falls back to the creation day
                n["valid"] = {"from": _date(src["valid_from"] if src.get("valid_from") is not None
                                            else src["created_at"]),
                              "to": _date(src["valid_to"]) if src.get("valid_to") is not None else None}
                if n.get("status") == "superseded":
                    n["superseded_by"] = n.pop("canonical_id", None)
                n["current"] = (not (n.get("status") == "superseded" or n.get("outdated_by"))) or bool(as_of)
            res["mode"] = "timeline"
            if as_of:
                res["as_of"] = as_of
        if not res.get("found"):
            res["hint"] = ("No such entity. Do not retry the same name; try memory_search with the topic words, or "
                           "continue without memory.")
        return json.dumps(res, ensure_ascii=False)

    def _update(self, a: dict) -> str:
        nid = int(a["id"])
        content = str(a.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        masked = looks_masked_secret(content) or looks_masked_secret(str(a.get("keywords") or ""))
        if masked:
            return json.dumps({"error": f"refused: {masked}; store a description without it", "refused": True},
                              ensure_ascii=False)
        kw = a.get("keywords")
        if kw is not None:
            kw = str(" ".join(kw) if isinstance(kw, (list, tuple)) else kw)
            kw = kw.replace(",", " ").replace("，", " ").replace("、", " ")
        before = self._store.get(nid) if self._config.get("remember_review") else None
        hold = self._hold_reason(content, kw or "", False) if before else ""
        if before and not hold and before.get("pinned") and self._config.get("remember_review") == "auto":
            hold = "it changes a pinned note"
        updated = self._store.update(nid, content, kw)
        res = {"id": nid, "updated": updated}
        if updated and hold:           # the old text stays in the note's history; the new text waits for the maintainer
            self._store.mark_pending(nid)
            res["review"] = "held for the maintainer to confirm" + (f" ({hold})" if hold != "all" else "")
        return json.dumps(res)

    def _hold_reason(self, content: str, keywords: str, pinned: bool) -> str:
        """Review policy for notes written through a tool by another agent: '' = save directly. Modes (config
        remember_review): unset = trusted writer; 'auto' = hold only what looks risky; 'all' = hold everything."""
        mode = self._config.get("remember_review")
        if mode == "all" or mode == "pending":
            return "all"
        if mode != "auto":
            return ""
        try:
            kinds = _find_pii(f"{content}\n{keywords}")
        except Exception:
            kinds = ()
        return write_flag(content, keywords, pinned, kinds)

    def _feedback(self, a: dict) -> str:
        nid = int(a["id"])
        trust = self._store.feedback(nid, _truthy(a.get("helpful")))
        if trust is None:
            return tool_error(f"no note #{nid}")
        self._learn_from_use(nid, helpful=_truthy(a.get("helpful")))
        self._store.log_event("feedback", note_id=nid, helpful=_truthy(a.get("helpful")), search_id=self._listing_for(nid))
        return json.dumps({"id": nid, "trust": trust, "hidden": trust < 0.2})

    def _forget(self, a: dict) -> str:
        nid = int(a["id"])
        return json.dumps({"id": nid, "deleted": self._store.forget(nid)})

    def _review(self, a: dict) -> str:
        action = str(a.get("action") or "list").lower()
        if action == "list":
            limit = max(1, min(int(a.get("limit") or 20), 100))
            if _truthy(a.get("rejected", False)):
                notes = self._store.list_rejected(limit)
                return json.dumps({"rejected": [_tiered(n, "more") for n in notes], "count": len(notes)},
                                  ensure_ascii=False)
            notes = self._store.list_pending(limit)
            total = self._store.pending_count()
            out = {"pending": [_tiered(n, "more") for n in notes], "count": len(notes), "total_pending": total}
            if total > len(notes):
                out["more"] = f"{total - len(notes)} more waiting; raise limit or confirm/reject these first."
            return json.dumps(out, ensure_ascii=False)
        if action not in ("confirm", "reject", "restore"):
            return tool_error("action must be list, confirm, reject or restore")
        ids = a.get("ids") if a.get("ids") is not None else ([a["id"]] if a.get("id") is not None else [])
        if isinstance(ids, (int, str)):
            ids = [ids]
        ids = [int(i) for i in ids]
        if not ids:
            return tool_error("id or ids is required")
        res = self._store.review_many(action, ids[:50])
        res["action"] = action
        if res["skipped"]:
            res["note"] = ("skipped = not in the state this action needs (confirm/reject: still [pending]; "
                           "restore: rejected). A confirmed note is removed with memory_forget.")
        return json.dumps(res, ensure_ascii=False)

    def _list(self, a: dict) -> str:
        notes = self._store.list(kind=a.get("kind") or None, limit=int(a.get("limit") or 20))
        return json.dumps({"results": [_public(n) for n in notes], "count": len(notes)}, ensure_ascii=False)


# Cue words of a settled decision / fact (Cantonese, written Chinese, English). Generic; no benchmark content.
_RE_CUE = re.compile(r"決定|决定|結論|结论|不建議|不建议|唔建議|改用|換成|换成|轉用|转用|定咗|揀咗|選擇了|选择了|以後都|"
                     r"之後都|統一用|统一用|唔好再|不要再|設定為|设定为|已經裝|已安装|已經改|"
                     r"\b(?:decided|decision|conclusion|agreed|switch(?:ed)? to|instead of|from now on|we'll use|"
                     r"going with|chose|settled on|don't use|do not use|configured|set to|installed|migrated)\b",
                     re.IGNORECASE)
_RE_FACTISH = re.compile(r"\d+(?:\.\d+)?\s*(?:GB|MB|TB|%|ms|秒|個|核|vCore|元|USD|HKD)|\b(?:v?\d+\.\d+(?:\.\d+)?|"
                         r"port\s*\d+|\d{1,3}(?:\.\d{1,3}){3})\b", re.IGNORECASE)
_RE_SENT = re.compile(r"(?<=[。！？!?；;\n])|(?<=\.)\s+")


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def pick_candidates(messages: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    """Sentences carrying a decision cue, or a cue-less tech/state sentence with a quantity/version/port, from the
    user/assistant messages (newest kept when over *limit*)."""
    out: List[str] = []
    for msg in messages:
        if msg.get("role") not in ("user", "assistant") or not isinstance(msg.get("content"), str):
            continue
        text = strip_injected(msg["content"])
        for sent in _RE_SENT.split(text):
            sent = " ".join(sent.split()).strip(" -*#>")
            if not (12 <= len(sent) <= 400) or looks_secret(sent):
                continue
            if _RE_CUE.search(sent) or (_RE_FACTISH.search(sent) and classify_kind(sent) in ("tech", "state",
                                                                                             "decision")):
                if sent not in out:
                    out.append(sent)
    return out[-limit:]


DecisionMemoryProvider = HermindProvider   # pre-1.0 name


def register(ctx) -> None:
    """Register the hermind provider with the plugin system."""
    ctx.register_memory_provider(HermindProvider(config=_load_plugin_config()))
