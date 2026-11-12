"""Offline consolidation into per-entity summary notes ('observations', an idea credited to Hindsight) for hermind.

For each entity with several notes, ask an OpenAI-compatible endpoint to write 1-4 statements of the CURRENT truth
(newer facts win, old values mentioned briefly), each citing source note ids. The result is stored as kind='summary'
(locked, high rank, '(綜合 #id ...)' suffix) next to the untouched source notes. Guards: sources must be real ids from
the prompt, and every number in a statement must occur in a cited source (no invented figures).

Idempotent: re-running an entity replaces its summaries. Never run from the search path.
  python tools/build_summaries.py --db PATH [--limit N] [--entities a,b] [--dry-run] [--log file]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import backfill_aliases as bf  # noqa: E402
from store import DecisionStore  # noqa: E402
from textnorm import numbers  # noqa: E402

PROMPT = """You maintain a personal long-term memory. Below are notes about one topic ("{entity}"), oldest first, each
with an id and date. Write 1-4 CONSOLIDATED statements of what is true NOW:
- merge related facts; when a value changed over time, state the latest value and mention the earlier one briefly;
- keep concrete names, numbers, dates and reasons from the notes; never add facts that are not in the notes;
- each statement under 200 characters, in the language, script and wording of the notes (do not convert Traditional and
  Simplified Chinese into each other), English product names unchanged;
- "sources": the ids of the notes the statement is based on.
Answer with JSON only: {{"summaries": [{{"text": "...", "sources": [id, ...]}}]}}

Notes:
"""


def _parse(text: str):
    t = bf.parse_items(text.replace('"summaries"', '"items"'))
    return t


def make_prompt(entity: str, notes: list[dict]) -> str:
    lines = []
    for n in notes:
        d = datetime.fromtimestamp(n["created_at"]).strftime("%Y-%m-%d") if n["created_at"] else "?"
        lines.append(json.dumps({"id": n["id"], "date": d, "note": n["content"][:500]}, ensure_ascii=False))
    return PROMPT.format(entity=entity) + "\n".join(lines)


def validate(items, notes: list[dict]) -> list[tuple[str, list[int]]]:
    by_id = {n["id"]: n for n in notes}
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        try:
            srcs = [int(i) for i in (it.get("sources") or []) if int(i) in by_id]
        except (TypeError, ValueError):
            continue
        if not text or len(text) > 300 or not srcs or bf.looks_secret(text):
            continue
        allowed = set()
        for i in srcs:
            allowed |= set(numbers(by_id[i]["content"]))
        if any(x not in allowed for x in numbers(text)):
            continue  # a figure that no cited note contains: likely invented
        out.append((text, srcs))
    return out


USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}


def call_usage(cfg, prompt: str) -> str:
    """Same request as bf.call_llm, but also accumulates token usage into USAGE."""
    import urllib.request
    # thinking mode is on by default on DeepSeek and would spend the whole max_tokens on reasoning (empty content),
    # so disable it; response_format=json_object keeps the answer parseable.
    body = json.dumps({"model": cfg.model, "temperature": 0.3, "max_tokens": 300 * cfg.batch_size,
                       "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    u = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    USAGE["completion_tokens"] += int(u.get("completion_tokens") or 0)
    USAGE["cached_tokens"] += int(u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return str(data["choices"][0]["message"]["content"] or "")


def work(cfg, entity: str, notes: list[dict]):
    prompt = make_prompt(entity, notes)
    err = None
    for mi, model in enumerate(bf._models(cfg)):
        c = argparse.Namespace(**{**vars(cfg), "model": model})
        for attempt in range(cfg.retries + 1 if mi == 0 else 1):
            try:
                items = _parse(call_usage(c, prompt))
                if items is not None:
                    return entity, notes, validate(items, notes), model, None
                err = f"{model}: malformed output"
            except Exception as exc:
                err = f"{model}: {type(exc).__name__}: {exc}"[:200]
            time.sleep(min(8.0, cfg.retry_wait * (2 ** attempt)))
    return entity, notes, None, None, err


def run(cfg, out=None) -> dict:
    out = out or sys.stdout
    cfg.concurrency = max(1, min(2, int(cfg.concurrency)))
    store = DecisionStore(cfg.db)
    stats = {"entities": 0, "written": 0, "empty": 0, "failed": 0, "dry_run": cfg.dry_run}
    try:
        if cfg.entities:
            targets = [(e.strip(), 0) for e in cfg.entities.split(",") if e.strip()]
        else:
            targets = store.summary_targets(cfg.min_notes, cfg.max_notes, stale_only=cfg.stale_only)
        if cfg.limit:
            targets = targets[:cfg.limit]

        def done(res):
            entity, notes, items, model, err = res
            stats["entities"] += 1
            if items is None:
                stats["failed"] += 1
                bf.log_event(out, event="failed", entity=entity, error=err)
            elif not items:
                stats["empty"] += 1
                bf.log_event(out, event="empty", entity=entity)
            else:
                if not cfg.dry_run:
                    store.replace_summaries(entity, items)
                    stats["written"] += len(items)
                bf.log_event(out, event="done" if not cfg.dry_run else "preview", entity=entity, model=model,
                             summaries=[t for t, _ in items])

        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            pending = []
            for entity, _ in targets:
                notes = store.entity_notes(entity, cfg.per_entity)
                if len(notes) < 2:
                    continue
                pending.append(pool.submit(work, cfg, entity, notes))
                if len(pending) >= cfg.concurrency:
                    done(pending.pop(0).result())
            for f in pending:
                done(f.result())
        stats["tokens"] = dict(USAGE)
        stats["cost_usd"] = round(USAGE["prompt_tokens"] * cfg.price_in / 1e6 +
                                  USAGE["completion_tokens"] * cfg.price_out / 1e6, 4)
        return stats
    finally:
        store.close()


def _env(name: str, default: str = "") -> str:
    """HERMIND_BACKFILL_<name>, or the older DM_BACKFILL_<name>."""
    return os.environ.get("HERMIND_BACKFILL_" + name) or os.environ.get("DM_BACKFILL_" + name) or default


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--base-url", default=_env("BASE_URL"))
    p.add_argument("--model", default=_env("MODEL"))
    p.add_argument("--fallback-model", default=_env("FALLBACK"))
    p.add_argument("--api-key-env", default="")
    p.add_argument("--min-notes", type=int, default=3)
    p.add_argument("--max-notes", type=int, default=40)
    p.add_argument("--per-entity", type=int, default=20, help="newest N notes of an entity go into the prompt")
    p.add_argument("--entities", default="", help="comma-separated entity names (default: all eligible)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--retry-wait", type=float, default=2.0)
    p.add_argument("--batch-size", type=int, default=8, help="(max_tokens = 300 x this)")
    p.add_argument("--stale-only", action="store_true", help="only entities with new notes since their last summary")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log", default="")
    p.add_argument("--price-in", type=float, default=0.0, help="USD per 1M prompt tokens (for the cost estimate)")
    p.add_argument("--price-out", type=float, default=0.0, help="USD per 1M completion tokens")
    return p


def main(argv=None) -> int:
    cfg = build_parser().parse_args(argv)
    if not cfg.base_url or not cfg.model:
        sys.exit("error: this tool talks to an OpenAI-compatible chat endpoint you choose: pass --base-url and --model "
                 "(or set HERMIND_BACKFILL_BASE_URL / HERMIND_BACKFILL_MODEL), and --api-key-env if it needs a key. "
                 "Nothing is sent anywhere until you do.")
    if cfg.log:
        with open(cfg.log, "a", encoding="utf-8") as fh:
            print(json.dumps(run(cfg, fh)))
    else:
        print(json.dumps(run(cfg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
