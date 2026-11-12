"""Offline alias backfill (doc2query) for hermind v0.4.

For notes whose agent-written `keywords` are empty or short, ask an OpenAI-compatible /chat/completions endpoint for
5-8 short alternative phrasings (how a user might ask for the fact in colloquial Cantonese, written Chinese and
English) plus 3-5 key terms, several notes per request. Results go to the `gen_keywords` column (+ gen_at,
gen_model) and are indexed as their own, lower-weighted posting field. Agent `keywords` are never touched.

Never run from the search path or maintenance(): run it by hand or from cron. Idempotent and resumable (notes that
already have gen_at are skipped; each batch is committed when it arrives). Malformed output is skipped per note.

Usage:
  python tools/backfill_aliases.py --db PATH --base-url http://host/v1 --model NAME
         [--fallback-model A,B] [--api-key-env VAR] [--batch-size 8] [--concurrency 2] [--timeout 30] [--retries 2] [--extra-body JSON]
         [--kinds decision,preference,tech,state] [--limit N] [--since YYYY-MM-DD|last] [--max-keywords 4]
         [--rebuild] [--dry-run] [--log progress.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from screen import looks_secret  # noqa: E402
from store import DecisionStore, _parse_when  # noqa: E402
from textnorm import normalize  # noqa: E402

MAX_ALIAS_CHARS = 40
MAX_ALIASES = 13          # 8 phrasings + 5 terms
META_LAST_RUN = "backfill_aliases.last_run"
DEFAULT_KINDS = "decision,preference,tech,state"

PROMPT = """You help a personal memory search engine. For each note below, write how a user might later ASK for this
fact, so that a keyword search finds the note even when the question shares no words with it.
For each note give:
- "aliases": 5-8 SHORT phrasings (each under 30 characters): colloquial Cantonese (e.g. 點算 / 部機 / 搞掂),
  written Chinese, and English. Questions or noun phrases, not copies of the note.
- "terms": 3-5 key terms (names, topic words, synonyms not in the note).
Answer with JSON only: {"items": [{"id": <note id>, "aliases": [...], "terms": [...]}, ...]}

Notes:
"""

_RE_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏  ﻿]")


def log_event(fh, **event) -> None:
    event.setdefault("ts", round(time.time(), 3))
    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    fh.flush()


def call_llm(cfg: argparse.Namespace, prompt: str) -> str:
    """One chat completion; raises on HTTP / network / timeout errors."""
    payload = {"model": cfg.model, "temperature": 0.3, "max_tokens": 300 * cfg.batch_size,
               "messages": [{"role": "user", "content": prompt}]}
    payload.update(json.loads(cfg.extra_body) if getattr(cfg, "extra_body", "") else {})   # e.g. switch a reasoning model's thinking off
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return str(data["choices"][0]["message"]["content"] or "")


def parse_items(text: str) -> list | None:
    """The items list from the model's answer (tolerates code fences / prose around the JSON); None if unusable."""
    if not text:
        return None
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, text, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("items")
        if isinstance(data, list):
            return data
    return None


def clean_aliases(item: dict, content: str) -> list[str]:
    """Validated aliases for one note: strings only, control characters stripped, length-capped, no copies of the
    note text, no secrets, deduplicated."""
    raw = []
    for key in ("aliases", "terms"):
        v = item.get(key)
        if isinstance(v, list):
            raw += [x for x in v if isinstance(x, str)]
    norm_content = re.sub(r"\s+", "", normalize(content))
    out, seen = [], set()
    for a in raw:
        a = unicodedata.normalize("NFKC", _RE_CTRL.sub(" ", a))
        a = " ".join(a.replace("|", " ").split()).strip(" \"'`,;")
        if not a or len(a) > MAX_ALIAS_CHARS or looks_secret(a):
            continue
        na = re.sub(r"\s+", "", normalize(a))
        # reject aliases that just copy the note (a long verbatim chunk or the whole text)
        if na == norm_content or (len(na) >= 12 and na in norm_content):
            continue
        if na in seen:
            continue
        seen.add(na)
        out.append(a)
    return out[:MAX_ALIASES]


def _models(cfg) -> list[str]:
    fb = [m.strip() for m in str(getattr(cfg, "fallback_model", "") or "").split(",") if m.strip()]
    return [cfg.model] + [m for m in fb if m != cfg.model]


def process_batch(cfg, batch: list[dict]) -> tuple[list[dict], dict | None, str | None]:
    """Returns (batch, {id: item}, error). Retries transport errors / timeouts / unparsable answers on the primary
    model, then tries each fallback model once (free models are flaky: 404, 429, malformed JSON). Each item carries
    '_model', the model that actually answered."""
    prompt = PROMPT + "\n".join(json.dumps({"id": n["id"], "note": n["content"][:600]}, ensure_ascii=False)
                                for n in batch)
    err = None
    for mi, model in enumerate(_models(cfg)):
        attempts = cfg.retries + 1 if mi == 0 else 1
        c = argparse.Namespace(**{**vars(cfg), "model": model})
        for attempt in range(attempts):
            try:
                items = parse_items(call_llm(c, prompt))
                if items is None:
                    err = f"{model}: malformed output"
                else:
                    out = {str(i.get("id")): {**i, "_model": model} for i in items if isinstance(i, dict)}
                    return batch, out, None
            except Exception as exc:  # timeout, HTTP error, connection refused, bad JSON envelope ...
                err = f"{model}: {type(exc).__name__}: {exc}"[:200]
            if attempt < attempts - 1:
                time.sleep(min(8.0, cfg.retry_wait * (2 ** attempt)))
    return batch, None, err


def run(cfg: argparse.Namespace, out=None) -> dict:
    out = out or sys.stdout
    if not cfg.base_url or not cfg.model:
        raise SystemExit("--base-url and --model are required")
    cfg.concurrency = max(1, min(2, int(cfg.concurrency)))
    store = DecisionStore(cfg.db)
    stats = {"examined": 0, "written": 0, "skipped": 0, "failed_batches": 0, "missing": 0, "dry_run": cfg.dry_run}
    started = time.time()
    try:
        kinds = [k.strip() for k in (cfg.kinds or "").split(",") if k.strip()]
        since = None
        if cfg.since == "last":
            last = store.get_meta(META_LAST_RUN)
            since = float(last) if last else None
        elif cfg.since:
            since = _parse_when(cfg.since)
        if cfg.rebuild and not cfg.dry_run:
            log_event(out, event="rebuild", cleared=store.clear_gen_keywords(kinds))
        log_event(out, event="start", kinds=kinds, since=since, limit=cfg.limit, dry_run=cfg.dry_run)

        def batches():
            after, left = 0, cfg.limit if cfg.limit else None
            while left is None or left > 0:
                page, last = store.backfill_targets(kinds, since=since, max_keywords=cfg.max_keywords,
                                                    after_id=after)
                if last is None:
                    return
                after = last
                if left is not None:
                    page = page[:left]
                    left -= len(page)
                for i in range(0, len(page), cfg.batch_size):
                    yield page[i:i + cfg.batch_size]

        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            pending = []
            for batch in batches():
                pending.append(pool.submit(process_batch, cfg, batch))
                if len(pending) >= cfg.concurrency:
                    _collect(cfg, store, pending.pop(0).result(), stats, out)
            for fut in pending:
                _collect(cfg, store, fut.result(), stats, out)
        if not cfg.dry_run:
            store.set_meta(META_LAST_RUN, str(started))
        stats["seconds"] = round(time.time() - started, 1)
        log_event(out, event="done", **stats)
        return stats
    finally:
        store.close()


def _collect(cfg, store: DecisionStore, result, stats: dict, out) -> None:
    batch, items, err = result
    stats["examined"] += len(batch)
    if items is None:
        stats["failed_batches"] += 1
        stats["skipped"] += len(batch)
        log_event(out, event="batch_failed", ids=[n["id"] for n in batch], error=err)
        return
    for n in batch:
        item = items.get(str(n["id"]))
        if item is None:
            stats["missing"] += 1
            log_event(out, event="note_missing", id=n["id"])  # stays undone; picked up by the next run
            continue
        aliases = clean_aliases(item, n["content"])
        if not aliases:
            stats["skipped"] += 1
            log_event(out, event="note_skipped", id=n["id"], reason="no valid aliases")
            continue
        if not cfg.dry_run:
            store.set_gen_keywords(n["id"], aliases, model=item.get("_model") or cfg.model)
        stats["written"] += 0 if cfg.dry_run else 1
        log_event(out, event="note_done" if not cfg.dry_run else "note_preview", id=n["id"], aliases=aliases)


def _env(name: str, default: str = "") -> str:
    """HERMIND_BACKFILL_<name>, or the older DM_BACKFILL_<name>."""
    return os.environ.get("HERMIND_BACKFILL_" + name) or os.environ.get("DM_BACKFILL_" + name) or default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--base-url", default=_env("BASE_URL"))
    p.add_argument("--model", default=_env("MODEL"))
    p.add_argument("--fallback-model", default=_env("FALLBACK"),
                   help="comma-separated models tried (once each) when the primary fails; '' = none")
    p.add_argument("--api-key-env", default="")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=2, help="parallel requests (max 2)")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--extra-body", default="", help='JSON merged into the request, e.g. \'{"thinking": {"type": "disabled"}}\' for DeepSeek')
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-wait", type=float, default=1.0)
    p.add_argument("--kinds", default=DEFAULT_KINDS, help="comma-separated; '' = all kinds")
    p.add_argument("--limit", type=int, default=0, help="at most N notes this run (0 = no limit)")
    p.add_argument("--since", default="", help="YYYY-MM-DD, or 'last' = notes added since the last run")
    p.add_argument("--max-keywords", type=int, default=4, help="notes with more agent keywords are skipped")
    p.add_argument("--rebuild", action="store_true", help="clear generated aliases (of --kinds) and regenerate")
    p.add_argument("--dry-run", action="store_true", help="call the endpoint and log proposals; write nothing")
    p.add_argument("--log", default="", help="JSON-lines progress log (default: stdout)")
    return p


def main(argv=None) -> int:
    cfg = build_parser().parse_args(argv)
    if not cfg.base_url or not cfg.model:
        sys.exit("error: this tool talks to an OpenAI-compatible chat endpoint you choose: pass --base-url and --model "
                 "(or set HERMIND_BACKFILL_BASE_URL / HERMIND_BACKFILL_MODEL), and --api-key-env if it needs a key. "
                 "Nothing is sent anywhere until you do.")
    cfg.batch_size = max(1, min(20, cfg.batch_size))
    if cfg.log:
        with open(cfg.log, "a", encoding="utf-8") as fh:
            run(cfg, fh)
    else:
        run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
