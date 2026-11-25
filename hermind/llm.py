"""Optional LLM layer for hermind (default OFF; imported only when `llm.enabled` is true).

Plain HTTP via urllib to an OpenAI-compatible /chat/completions endpoint. Every call is best-effort: any error,
timeout or unparsable answer returns None and the caller falls back to the deterministic path. No threads, no
state besides the config. The API key (optional) is read from the environment variable named in `api_key_env`.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

try:
    from .pii import find_pii, redact_pii
    from .screen import looks_secret, strip_private
except ImportError:  # top-level module load
    from pii import find_pii, redact_pii
    from screen import looks_secret, strip_private

# Sent as the User-Agent of every LLM request. It must equal the version in pyproject.toml, hermind/plugin.yaml and
# hermind_mcp.__version__; tests/test_mcp_packaging.py fails when they drift (1.1.0 shipped "Hermind/1.4").
_VERSION = "1.1.2"
USER_AGENT = f"Hermind/{_VERSION} (+https://github.com/fungane/hermind)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(req, timeout):
    # A loopback model must remain local even when a proxy has been configured.
    handlers = [_NoRedirect()]
    if urllib.parse.urlsplit(req.full_url).scheme == "http":
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def safe_endpoint(url):
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            return False
        if parts.scheme == "https":
            return True
        if parts.scheme != "http":
            return False
        return parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


class LLMClient:
    def __init__(self, cfg: dict):
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self.model = str(cfg.get("model") or "")
        self.timeout = float(cfg.get("timeout") or 8)
        self.session_id = uuid.uuid4().hex  # stable per client; the OpenCode relay routes on x-opencode-session
        self.api_key = os.environ.get(str(cfg.get("api_key_env") or ""), "") if cfg.get("api_key_env") else ""
        # extra request fields for providers that need them, e.g. DeepSeek: {"thinking": {"type": "disabled"}}
        self.extra_body = cfg.get("extra_body") if isinstance(cfg.get("extra_body"), dict) else {}
        # personal data in a prompt (email, phone, ID and payment-card numbers, plus the user's own regexes in
        # `redact_patterns`): "redact" replaces it with a tag (default), "block" skips the call, "off" sends as is
        mode = str(cfg.get("pii") or "redact").strip().lower()
        self.pii_mode = mode if mode in ("redact", "block", "off") else "redact"
        patterns = cfg.get("redact_patterns")
        self.redact_patterns = [str(p) for p in patterns] if isinstance(patterns, list) else []
        self.pii_redacted = 0          # prompts changed so far (for reports)
        self.pii_blocked = 0
        self.rerank_on = bool(cfg.get("rerank", False))
        self.rerank_candidates = max(5, min(30, int(cfg.get("rerank_candidates") or 20)))
        self.last_usage: dict = {}
        self.capture_extract = bool(cfg.get("capture_extract", False))
        # circuit breaker: after 3 failures in a row the endpoint is skipped for `cooldown` seconds, so an outage or an
        # exhausted balance costs nothing per search (callers fall back to the lexical / raw-chunk behaviour)
        self.cooldown = float(cfg.get("cooldown") or 300)
        self._fails = 0
        self._skip_until = 0.0

    def public_text(self, value):
        text = strip_private(value)[0]
        if looks_secret(text):
            return None
        if self.pii_mode == "block" and find_pii(text, self.redact_patterns):
            self.pii_blocked += 1
            return None
        if self.pii_mode == "redact":
            text, found = redact_pii(text, self.redact_patterns)
            self.pii_redacted += bool(found)
        return text

    def chat(self, prompt: str, max_tokens: int = 400, json_mode: bool = False) -> str | None:
        if not (self.base_url and self.model):
            return None
        if time.time() < self._skip_until:
            return None
        if not safe_endpoint(self.base_url):
            return None
        prompt = self.public_text(prompt)
        if prompt is None or self.public_text(self.model) != self.model:
            return None
        # Extra fields are operator configuration. Reject sensitive values and prevent
        # them from replacing the screened model, messages or response limits.
        extra = self.extra_body
        if {"model", "messages", "max_tokens", "input", "prompt"} & extra.keys():
            return None
        encoded_extra = json.dumps(extra, ensure_ascii=False)
        if self.public_text(encoded_extra) != encoded_extra:
            return None
        body = json.dumps({**extra, "model": self.model, "temperature": extra.get("temperature", 0),
                           "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}],
                           **({"response_format": {"type": "json_object"}} if json_mode else {})}).encode()
        headers = {"Content-Type": "application/json",
                   # a urllib default UA is blocked by opencode.ai's Cloudflare (1010)
                   "User-Agent": USER_AGENT}
        try:
            req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers=headers)
            if self.api_key:
                req.add_unredirected_header("Authorization", f"Bearer {self.api_key}")
            endpoint = urllib.parse.urlsplit(req.full_url)
            if endpoint.scheme == "https" and endpoint.hostname == "opencode.ai":
                req.add_unredirected_header("x-opencode-session", self.session_id)
            with _send(req, timeout=self.timeout) as resp:
                raw = resp.read(2_000_001)
                if len(raw) > 2_000_000:
                    return None
                data = json.loads(raw.decode("utf-8", "replace"))
            self.last_usage = data.get("usage") or {}
            self._fails = 0
            return str(data["choices"][0]["message"]["content"] or "").strip() or None
        except Exception:
            self._fails += 1
            if self._fails >= 3:
                self._skip_until = time.time() + self.cooldown
                self._fails = 0
            return None

    RERANK_PROMPT = ("A user asked a question about their own past notes. Below are candidate notes (id: text). Pick the "
                     "notes that help answer the question, best first (at most 5). Judge by meaning, not by shared "
                     "words. If none help, return an empty list.\nAnswer JSON only: {{\"ids\": [id, ...]}}\n\n"
                     "Question: {q}\n\nCandidates:\n{c}")

    def rerank(self, query: str, notes: list[dict], chars: int = 180) -> list[int] | None:
        """Ids (subset of notes, best first) the model judges relevant; None on any failure (caller keeps the
        lexical order). An empty list means 'none helps' and is also treated as no information by the caller."""
        if not notes:
            return None
        cands = []
        for n in notes:
            text = self.public_text(str(n["content"]))
            if text is None:
                return None
            cands.append(f"{n['id']}: {text[:chars].replace(chr(10), ' ')}")
        cands = "\n".join(cands)
        out = self.chat(self.RERANK_PROMPT.format(q=query, c=cands), max_tokens=200, json_mode=True)
        if not out:
            return None
        m = re.search(r"\{.*\}", out, re.DOTALL)
        try:
            ids = json.loads(m.group(0)).get("ids") if m else None
            valid = {int(n["id"]) for n in notes}
            return [i for i in dict.fromkeys(int(x) for x in ids) if i in valid] if isinstance(ids, list) else None
        except (ValueError, TypeError, AttributeError):
            return None

    def _json_list(self, prompt: str, max_tokens: int = 400) -> list[str] | None:
        out = self.chat(prompt, max_tokens)
        if not out:
            return None
        m = re.search(r"\[.*\]", out, re.DOTALL)
        try:
            items = json.loads(m.group(0)) if m else None
        except ValueError:
            return None
        if not isinstance(items, list):
            return None
        return [str(x).strip() for x in items if isinstance(x, (str, int, float)) and str(x).strip()]

    def extract_entities(self, text: str) -> list[str] | None:
        """Main named entities with aliases, as ['Name=alias1|alias2', ...]."""
        text = self.public_text(text)
        if text is None:
            return None
        items = self._json_list(
            "List the main named entities (products, hosts, people, projects, models) in this note as a JSON array "
            "of strings 'Name=alias1|alias2' (aliases: other spellings, Cantonese / Chinese / English names). "
            "Output only the JSON array.\n\nNote:\n" + text[:2000], 300)
        return [x for x in items if len(x) <= 120][:12] if items is not None else None

    def extract_candidates(self, transcript: str) -> list[str] | None:
        """Self-contained decision / fact sentences from a conversation excerpt."""
        transcript = self.public_text(transcript)
        if transcript is None:
            return None
        items = self._json_list(
            "From this conversation excerpt, extract up to 8 self-contained facts worth remembering long-term: "
            "decisions and their reasons, settled preferences, technical setup and dated states. Keep the original "
            "language. Output only a JSON array of strings.\n\n" + transcript[-6000:], 600)
        return [x for x in items if 12 <= len(x) <= 400][:8] if items is not None else None

    FACTS_PROMPT = (
        "You maintain a user's long-term memory. Most conversation turns add nothing worth keeping: return an empty "
        "list unless this turn SETTLES something. Usually 0 or 1 fact; at most 3, only for separate decisions.\n"
        "Keep: decisions and their reason; the user's preferences and corrections, including ones said in passing "
        "('my English is weak, keep it simple'); the cause of a problem once found; durable facts about the user, their "
        "accounts, machines and setup (paths, versions, hardware, prices they pay); outcomes of finished work (what was "
        "built, fixed or installed, and where it lives).\n"
        "Skip: progress narration ('the user is working on…', 'next step is…', 'the assistant ran / checked…'); plans "
        "or options not yet chosen; the assistant's general explanations, advice or research results that are not about "
        "the user's own setup; what the user merely asked about; chit-chat; process ids, one-off command output and "
        "anything stale within a week. Several facts about the same thing: merge them into one.\n"
        "Chinese text keeps the script and regional wording the user wrote in (Traditional stays Traditional, Simplified "
        "stays Simplified); never convert between them.\n"
        "Each fact: a self-contained statement (names, numbers, dates, the reason) written in the language the USER "
        "wrote in (a Chinese-speaking user gets Chinese facts even when the reply was in English); "
        "kind = decision | preference | tech | state | chat; aliases = 5-8 SHORT alternative phrasings someone might "
        "later use to ask about it (colloquial, formal, English), not copies of the text.\n"
        "JSON only: "
        "{{\"facts\": [{{\"text\": str, \"kind\": str, \"aliases\": [str]}}]}}\n\nTurn:\n{turn}")

    def extract_facts(self, user: str, assistant: str) -> list[dict] | None:
        """Durable facts of one turn as [{text, kind, aliases}]; None when the model failed (caller falls back to the
        raw chunk), [] when it judged the turn not worth keeping."""
        user, assistant = self.public_text(user), self.public_text(assistant)
        if user is None or assistant is None:
            return None
        turn = f"User: {user[:1500]}\nAssistant: {assistant[:1500]}"
        out = self.chat(self.FACTS_PROMPT.format(turn=turn), max_tokens=700, json_mode=True)
        if not out:
            return None
        m = re.search(r"\{.*\}", out, re.DOTALL)
        try:
            facts = json.loads(m.group(0)).get("facts") if m else None
        except (ValueError, AttributeError):
            return None
        if not isinstance(facts, list):
            return None
        clean = []
        for f in facts[:3]:
            if not isinstance(f, dict):
                continue
            text = str(f.get("text") or "").strip()
            if not 12 <= len(text) <= 400:
                continue
            al = f.get("aliases") if isinstance(f.get("aliases"), list) else []
            al = [str(a).strip()[:40] for a in al if isinstance(a, (str, int, float)) and str(a).strip()][:8]
            clean.append({"text": text, "kind": str(f.get("kind") or "").strip().lower(), "aliases": al})
        return clean

    REVIEW_PROMPT = (
        "You tidy a user's long-term memory while they sleep. Below are RECENT notes (to review) and OLDER notes "
        "(context only), each as 'id | kind | date | text'. Propose only changes you are sure about; an empty answer is "
        "normal.\n"
        "1. merges: two notes that state the same fact. keep = the more complete one, drop = the redundant ones. The "
        "kept note must contain every number of the dropped one.\n"
        "2. supersessions: an older note whose fact was replaced by a newer note about the same thing (a changed value, "
        "a reversed decision). old = the outdated note, new = the newer one. Only when the two notes cannot both be "
        "true now: if the old note is still true (a test result, a past measurement, a reason, a list of what was "
        "done) and the newer note merely adds to it, concerns another decision or another item, or records a later "
        "step, do NOT supersede it.\n"
        "3. aggregates: a NEW fact that only becomes visible by combining two or more notes (a pattern, a total, a "
        "preference shown repeatedly). text = one self-contained statement under 200 characters, in the language of "
        "the sources (Chinese in the same script and wording as the sources, never converted), using only numbers that occur in the "
        "sources; sources = the ids it is based on; kind = decision | preference | tech | state | chat. Never list "
        "something as a combined total when a source note says one item replaced another (replaced is not added).\n"
        "Use only ids that appear below. Never invent facts.\n"
        "JSON only: {{\"merges\": [{{\"keep\": id, \"drop\": [id], \"reason\": str}}], "
        "\"supersessions\": [{{\"old\": id, \"new\": id, \"reason\": str}}], "
        "\"aggregates\": [{{\"text\": str, \"kind\": str, \"sources\": [id], \"reason\": str}}]}}\n\n{notes}")

    def review_batch(self, notes_text: str) -> dict | None:
        """Sleep-time review of recent notes (tools/sleep_consolidate.py): {'merges', 'supersessions', 'aggregates'}
        lists as the model returned them (the caller validates every id and figure); None when the model failed."""
        out = self.chat(self.REVIEW_PROMPT.format(notes=notes_text), max_tokens=4000, json_mode=True)
        if not out:
            return None
        m = re.search(r"\{.*\}", out, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return {k: [x for x in (data.get(k) or []) if isinstance(x, dict)] if isinstance(data.get(k) or [], list) else []
                for k in ("merges", "supersessions", "aggregates")}

    DIGEST_PROMPT = (
        "Below are memory notes (id, kind, date, text), oldest first. Today is {today}.\n\n{notes}\n\n"
        "Question: {q}\n\nTask: list every fact from the notes that could help answer the question, as short bullet "
        "points, each prefixed with its date and note id like '[2026-10-17 #12]'. Keep exact numbers, names, prices and "
        "dates, plus personal details, habits and preferences that relate to the topic. When the same quantity changes "
        "over time, list every value with its date and mark the newest as CURRENT. Do not answer the question. If "
        "nothing is relevant, write NONE.")

    def synthesize(self, query: str, pack: dict) -> str | None:
        """Dated fact digest (oldest first) of the notes in a reflect pack, for the caller to reason over. Counting,
        date arithmetic and 'use the newest value' are left to the reader; on the LongMemEval-S benchmark this
        extract-then-answer split raised answer accuracy far above feeding raw notes (see docs/how-it-works.md)."""
        rows = {n["id"]: n for g in pack.get("groups", []) for n in g["notes"]}
        if not rows:
            return None
        notes = "\n".join(f"#{n['id']} [{n['kind']} {n['date']}] {n['content']}"
                          for n in sorted(rows.values(), key=lambda n: (n["date"], n["id"])))
        notes = self.public_text(notes)
        if notes is None:
            return None
        return self.chat(self.DIGEST_PROMPT.format(today=time.strftime("%Y-%m-%d"), notes=notes[:12000], q=query), 700)
