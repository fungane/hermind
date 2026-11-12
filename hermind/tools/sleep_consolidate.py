#!/usr/bin/env python3
"""Sleep-time review of recent notes (hermind). Optional, needs an LLM endpoint; never runs by itself.

For the notes written in the last N hours the LLM proposes
  * merges           two notes stating the same fact (the redundant one is hidden behind the kept one),
  * supersessions    an older note replaced by a newer one (its valid_to is set, it leaves the default search),
  * aggregate facts  new facts only visible across several notes, stored as NEW *pending* notes that cite their
                     source ids (they rank lower until confirmed with memory_review).
Every id the model names must be a real note it was shown, every figure in an aggregate must occur in a source, a
merge may not lose a number, pinned / locked / summary notes are never hidden, and a pending note never hides a
confirmed one. NOTHING IS DELETED: merged and superseded notes stay in the database and come back with
`--restore ID` (or store.restore); an unwanted aggregate is rejected with memory_review (soft forget).

    python tools/sleep_consolidate.py --db ~/.hermes/hermind.db            # dry run (default): report only
    python tools/sleep_consolidate.py --db ... --apply [--since-hours 24] [--limit 30]
    python tools/sleep_consolidate.py --db ... --restore 45 52

LLM settings (same fields as plugins.hermind.llm; this tool does not read config.yaml): --llm-config FILE.json, or
--llm-base-url / --llm-model / --llm-api-key-env, or HERMIND_LLM_BASE_URL / HERMIND_LLM_MODEL /
HERMIND_LLM_API_KEY_ENV. Without a base URL and model it prints a message and exits 0. The text of the reviewed
notes is sent to that endpoint, so point it at a provider you already use for the plugin's LLM layer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from store import DecisionStore, KINDS, l0, numbers  # noqa: E402
from screen import looks_secret  # noqa: E402

BATCH = 10            # recent notes per LLM call
RELATED = 3           # older similar notes shown with each recent note, for context
NOTE_CHARS = 400


def _default_db() -> str:
    """$HERMES_HOME/hermind.db, or the old decision_memory.db when that is what is there."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    old, new = os.path.join(home, "decision_memory.db"), os.path.join(home, "hermind.db")
    return old if os.path.exists(old) and not os.path.exists(new) else new


def day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def build_llm(args, env=os.environ):
    """LLMClient from flags / JSON file / environment, or None when no endpoint + model is configured."""
    cfg: dict = {}
    if args.llm_config:
        cfg.update(json.loads(Path(args.llm_config).expanduser().read_text(encoding="utf-8")))
    for key, flag, var in (("base_url", args.llm_base_url, "HERMIND_LLM_BASE_URL"),
                           ("model", args.llm_model, "HERMIND_LLM_MODEL"),
                           ("api_key_env", args.llm_api_key_env, "HERMIND_LLM_API_KEY_ENV")):
        value = flag or env.get(var)
        if value:
            cfg[key] = value
    if not (cfg.get("base_url") and cfg.get("model")):
        return None
    cfg.setdefault("timeout", 60)
    from llm import LLMClient
    return LLMClient(cfg)


def collect(store: DecisionStore, since_hours: float, limit: int, now: float) -> list[dict]:
    """Recent active notes (newest first, at most *limit*) minus summaries and anything that looks like a secret."""
    rows = store.conn.execute(
        "SELECT * FROM notes WHERE status='active' AND kind!='summary' AND created_at >= ? ORDER BY id DESC LIMIT ?",
        (now - since_hours * 3600.0, int(limit) * 3)).fetchall()
    out = [store._row(r) for r in rows if not looks_secret(r["content"])]
    return out[:int(limit)]


def related_notes(store: DecisionStore, note: dict, now: float) -> list[dict]:
    from store import core_text
    hits = store.search(core_text(note["content"])[:300], limit=RELATED + 2, now=now, track=False, scope="*")
    return [h for h in hits if h["id"] != note["id"] and h["created_at"] <= note["created_at"]][:RELATED]


def render_note(n: dict) -> str:
    text = " ".join(n["content"].split())[:NOTE_CHARS]
    return f"{n['id']} | {n['kind']} | {day(n['created_at'])} | {text}"


def build_batches(store: DecisionStore, recent: list[dict], now: float) -> list[tuple[str, dict]]:
    """[(prompt text, {id: note})] - one entry per LLM call; the dict is every note the model may name."""
    batches = []
    for i in range(0, len(recent), BATCH):
        chunk = recent[i:i + BATCH]
        known = {n["id"]: n for n in chunk}
        older: dict[int, dict] = {}
        for n in chunk:
            for r in related_notes(store, n, now):
                if r["id"] not in known:
                    older.setdefault(r["id"], r)
        known.update(older)
        text = ("RECENT notes:\n" + "\n".join(render_note(n) for n in chunk) +
                ("\n\nOLDER notes (context):\n" + "\n".join(render_note(n) for n in older.values()) if older else ""))
        batches.append((text, known))
    return batches


def _ints(values) -> list[int]:
    out = []
    for v in values if isinstance(values, list) else [values]:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def validate(raw: dict, known: dict[int, dict], recent_ids: set[int], store: DecisionStore) -> dict:
    """Split the model's proposals into accepted ('ok') and rejected (with a reason). Pure apart from reads."""
    res = {"merges": [], "supersessions": [], "aggregates": []}

    def refuse(kind: str, item: dict, why: str) -> None:
        res[kind].append({"ok": False, "why": why, **item})

    def hideable(n: dict) -> str | None:
        if n["status"] != "active":
            return "not active"
        if n.get("pinned") or n.get("kind") == "summary" or n.get("locked"):
            return "pinned / summary / locked notes are never hidden"
        return None

    for m in raw.get("merges", []):
        keep, drops = _ints(m.get("keep"))[:1], _ints(m.get("drop"))
        for d in drops:
            item = {"keep": keep[0] if keep else None, "drop": d, "reason": str(m.get("reason") or "")[:200]}
            if not keep or keep[0] not in known or d not in known:
                refuse("merges", item, "unknown note id"); continue
            k, x = known[keep[0]], known[d]
            if k["id"] == x["id"]:
                refuse("merges", item, "keep and drop are the same note"); continue
            if d not in recent_ids and k["id"] not in recent_ids:
                refuse("merges", item, "neither note is recent"); continue
            why = hideable(x) or (None if k["status"] == "active" else "keep note is not active")
            if why:
                refuse("merges", item, why); continue
            if k.get("scope") != x.get("scope"):
                refuse("merges", item, "different scopes"); continue
            if k.get("review") == "pending" and x.get("review") != "pending":
                refuse("merges", item, "would hide a confirmed note behind an unconfirmed one"); continue
            if not numbers(x["content"]) <= numbers(k["content"]):
                refuse("merges", item, "the kept note lacks a number of the dropped one"); continue
            res["merges"].append({"ok": True, **item})
    for s in raw.get("supersessions", []):
        old, new = (_ints(s.get("old")) or [None])[0], (_ints(s.get("new")) or [None])[0]
        item = {"old": old, "new": new, "reason": str(s.get("reason") or "")[:200]}
        if old not in known or new not in known or old == new:
            refuse("supersessions", item, "unknown note id"); continue
        o, n = known[old], known[new]
        why = hideable(o) or (None if n["status"] == "active" else "newer note is not active")
        if why:
            refuse("supersessions", item, why); continue
        if o.get("scope") != n.get("scope"):
            refuse("supersessions", item, "different scopes"); continue
        t_new, t_old = n.get("event_at") or n["created_at"], o.get("valid_from") or o["created_at"]
        if t_new <= t_old:
            refuse("supersessions", item, "the 'newer' note is not newer"); continue
        if n.get("review") == "pending" and o.get("review") != "pending":
            refuse("supersessions", item, "would hide a confirmed note behind an unconfirmed one"); continue
        res["supersessions"].append({"ok": True, **item})
    for a in raw.get("aggregates", []):
        text = " ".join(str(a.get("text") or "").split())
        srcs = [i for i in dict.fromkeys(_ints(a.get("sources"))) if i in known]
        kind = str(a.get("kind") or "").strip().lower()
        item = {"text": text, "kind": kind if kind in KINDS and kind not in ("summary", "news") else "chat",
                "sources": srcs, "reason": str(a.get("reason") or "")[:200]}
        if not 12 <= len(text) <= 300:
            refuse("aggregates", item, "text length"); continue
        if len(srcs) < 2:
            refuse("aggregates", item, "needs at least two real source notes"); continue
        if looks_secret(text):
            refuse("aggregates", item, "looks like a secret"); continue
        allowed = set().union(*(numbers(known[i]["content"]) for i in srcs))
        if any(x not in allowed for x in numbers(text)):
            refuse("aggregates", item, "contains a figure no source has"); continue
        if store.is_forgotten(text):
            refuse("aggregates", item, "forgotten earlier"); continue
        if any(x["similarity"] >= 0.85 for x in store.write_check(text)["similar"]):
            refuse("aggregates", item, "already stored"); continue
        res["aggregates"].append({"ok": True, **item})
    return res


def merge_plans(parts: list[dict]) -> dict:
    """Combine the per-batch results; drop later duplicates (same pair / same text) and conflicting claims on a note."""
    out = {"merges": [], "supersessions": [], "aggregates": []}
    hidden: set[int] = set()
    texts: set[str] = set()
    for p in parts:
        for m in p["merges"]:
            if m["ok"] and (m["drop"] in hidden or m["keep"] in hidden):
                m = {**m, "ok": False, "why": "conflicts with another proposal in this run"}
            elif m["ok"]:
                hidden.add(m["drop"])
            out["merges"].append(m)
        for s in p["supersessions"]:
            if s["ok"] and (s["old"] in hidden or s["new"] in hidden):
                s = {**s, "ok": False, "why": "conflicts with another proposal in this run"}
            elif s["ok"]:
                hidden.add(s["old"])
            out["supersessions"].append(s)
        for a in p["aggregates"]:
            if a["ok"] and a["text"] in texts:
                a = {**a, "ok": False, "why": "duplicate proposal"}
            elif a["ok"]:
                texts.add(a["text"])
            out["aggregates"].append(a)
    return out


def apply_plan(store: DecisionStore, plan: dict, now: float, max_apply: int, tag: str) -> dict:
    """Perform the accepted proposals (at most *max_apply*). Returns what was done, ids included."""
    done = {"merged": [], "superseded": [], "aggregates": [], "skipped_over_limit": 0, "failed": []}
    budget = max_apply
    for m in plan["merges"]:
        if not m["ok"]:
            continue
        if budget <= 0:
            done["skipped_over_limit"] += 1; continue
        budget -= 1
        if store.fold_notes(m["drop"], m["keep"], "merged", now):
            done["merged"].append(m["drop"])
        else:
            done["failed"].append(("merge", m["drop"]))
    for s in plan["supersessions"]:
        if not s["ok"]:
            continue
        if budget <= 0:
            done["skipped_over_limit"] += 1; continue
        budget -= 1
        if store.fold_notes(s["old"], s["new"], "superseded", now):
            done["superseded"].append(s["old"])
        else:
            done["failed"].append(("supersede", s["old"]))
    for a in plan["aggregates"]:
        if not a["ok"]:
            continue
        if budget <= 0:
            done["skipped_over_limit"] += 1; continue
        budget -= 1
        cited = " ".join(f"#{i}" for i in a["sources"])
        try:
            nid, created = store.add(f"{a['text']} (綜合 {cited})", kind=a["kind"], review="pending", tags="sleep-consolidate",
                                     source_session=tag, proof_count=1, dedup_threshold=0.85)
        except ValueError as exc:      # RefusedWrite is a ValueError: forgotten / secret
            done["failed"].append(("aggregate", str(exc)[:60])); continue
        if created:
            done["aggregates"].append(nid)
        else:
            done["failed"].append(("aggregate", "already stored"))
    return done


def render_report(plan: dict, known: dict[int, dict], meta: dict, done: dict | None) -> str:
    def short(i):
        n = known.get(i)
        return f"#{i} [{n['kind']} {day(n['created_at'])}] {l0(n['content'], 70)}" if n else f"#{i} (unknown)"

    mode = "APPLIED" if done is not None else "DRY RUN (nothing was changed)"
    lines = [f"sleep_consolidate - {mode}",
             f"db: {meta['db']}   window: last {meta['since_hours']:g} h   reviewed: {meta['reviewed']} notes in "
             f"{meta['batches']} LLM call(s), {meta['failed_batches']} failed",
             ""]
    for title, key in (("MERGES", "merges"), ("SUPERSESSIONS (older note gets valid_to)", "supersessions"),
                       ("AGGREGATE FACTS (new pending notes)", "aggregates")):
        items = plan[key]
        lines.append(f"{title}: {sum(1 for x in items if x['ok'])} accepted, {sum(1 for x in items if not x['ok'])} rejected")
        for x in items:
            tag = "ok      " if x["ok"] else f"rejected ({x['why']})"
            if key == "merges":
                lines += [f"  [{tag}] hide {short(x['drop'])}", f"      behind {short(x['keep'])}"]
            elif key == "supersessions":
                lines += [f"  [{tag}] {short(x['old'])}", f"      replaced by {short(x['new'])}"]
            else:
                lines += [f"  [{tag}] ({x['kind']}) {x['text']}", f"      from {' '.join('#%d' % i for i in x['sources'])}"]
            if x.get("reason"):
                lines.append(f"      why: {x['reason']}")
        lines.append("")
    if done is None:
        n_ok = sum(1 for k in plan for x in plan[k] if x["ok"])
        lines.append(f"{n_ok} change(s) would be applied with --apply." if n_ok else "Nothing to apply.")
    else:
        lines.append(f"applied: merged {len(done['merged'])}, superseded {len(done['superseded'])}, "
                     f"new pending notes {len(done['aggregates'])} {done['aggregates'] or ''}")
        if done["skipped_over_limit"]:
            lines.append(f"{done['skipped_over_limit']} accepted proposal(s) left for the next run (--max-apply).")
        if done["failed"]:
            lines.append(f"not applied (state changed meanwhile): {done['failed']}")
        undo = done["merged"] + done["superseded"]
        if undo:
            lines.append("undo merged / superseded notes:  python tools/sleep_consolidate.py --db DB --restore "
                         + " ".join(str(i) for i in undo))
        if done["aggregates"]:
            lines.append("review the new notes with memory_review (list / confirm / reject): ids "
                         + " ".join(str(i) for i in done["aggregates"]))
    return "\n".join(lines)


def run(args, llm=None, out=None, now: float | None = None) -> int:
    out = out or sys.stdout
    now = time.time() if now is None else now
    db = Path(args.db).expanduser()
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 2
    if args.restore:
        store = DecisionStore(str(db))
        try:
            for i in args.restore:
                print(f"#{i}: {'restored' if store.restore(i) else 'nothing to restore'}", file=out)
        finally:
            store.close()
        return 0
    llm = llm if llm is not None else build_llm(args)
    if llm is None:
        print("No LLM configured (give --llm-base-url and --llm-model, or --llm-config FILE, or set "
              "HERMIND_LLM_BASE_URL / HERMIND_LLM_MODEL). Nothing to do.", file=out)
        return 0
    store = DecisionStore(str(db))
    try:
        recent = collect(store, args.since_hours, args.limit, now)
        if not recent:
            print(f"No active notes created in the last {args.since_hours:g} hours. Nothing to do.", file=out)
            return 0
        batches = build_batches(store, recent, now)
        parts, known, failed = [], {}, 0
        recent_ids = {n["id"] for n in recent}
        for text, ctx in batches:
            known.update(ctx)
            raw = None
            try:
                raw = llm.review_batch(text)
            except Exception:
                raw = None
            if raw is None:
                failed += 1
                continue
            parts.append(validate(raw, ctx, recent_ids, store))
        plan = merge_plans(parts)
        done = None
        if args.apply and any(x["ok"] for k in plan for x in plan[k]):
            done = apply_plan(store, plan, now, args.max_apply, f"sleep:{day(now)}")
        elif args.apply:
            done = {"merged": [], "superseded": [], "aggregates": [], "skipped_over_limit": 0, "failed": []}
        meta = {"db": str(db), "since_hours": args.since_hours, "reviewed": len(recent), "batches": len(batches),
                "failed_batches": failed}
        print(render_report(plan, known, meta, done), file=out)
        if failed == len(batches):
            print("\nThe LLM gave no usable answer (unreachable, rate-limited or malformed); nothing was changed.",
                  file=out)
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=_default_db())
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="perform the accepted proposals")
    p.add_argument("--since-hours", type=float, default=24.0, help="review notes created in the last N hours")
    p.add_argument("--limit", type=int, default=30, help="at most N recent notes per run")
    p.add_argument("--max-apply", type=int, default=20, help="at most N changes per --apply run")
    p.add_argument("--restore", type=int, nargs="+", metavar="ID", help="undo a merge / supersession and exit")
    p.add_argument("--llm-config", default="", help="JSON file with the llm settings (base_url, model, api_key_env, ...)")
    p.add_argument("--llm-base-url", default="")
    p.add_argument("--llm-model", default="")
    p.add_argument("--llm-api-key-env", default="", help="NAME of the environment variable holding the API key")
    return p


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
