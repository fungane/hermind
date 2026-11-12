#!/usr/bin/env python3
"""Show how recall is doing in real use: searches, empty results, rewrites, feedback.
usage: python3 tools/usage_report.py PATH/TO/hermind.db [--days 30]"""
import argparse, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import DecisionStore

ap = argparse.ArgumentParser()
ap.add_argument("db"); ap.add_argument("--days", type=int, default=30)
a = ap.parse_args()
r = DecisionStore(a.db, track_access=False).usage_report(a.days)
print(f"last {r['days']} days, {r['notes']} notes in the database")
for src, v in r["searches"].items():
    label = {"tool": "searches made by the agent (memory_search)", "auto": "automatic lookups before each reply"}.get(src, src)
    pct = 100 * v["zero_result"] / v["count"] if v["count"] else 0
    print(f"- {label}: {v['count']}  | no result: {v['zero_result']} ({pct:.0f}%)  | with rewrites: {v['with_rewrites']}")
if not r["searches"]:
    print("- no searches logged yet")
for k, v in r.get("llm", {}).items():
    label = {"rerank": "LLM rerank of search results", "extract": "LLM fact extraction after a reply"}[k]
    print(f"- {label}: {v['calls']} calls, {v['prompt_tokens']} prompt tokens, {v['no_answer']} without usable answer")
print(f"- notes opened with memory_get: {r.get('gets', 0)} (also feeds term learning)")
fb = r["feedback"]
print(f"- feedback: helpful {fb.get('helpful', 0)}, not helpful {fb.get('not_helpful', 0)}")
if r["recent_zero_result_queries"]:
    print("recent searches that found nothing:")
    for q in r["recent_zero_result_queries"]:
        print("   ", q)
orr = r.get("open_rate") or {}
if orr.get("tracked"):
    print(f"- open-rate prior: {orr['tracked']} notes shown at least once, {orr['shown_total']} shown / "
          f"{orr['opened_total']} opened in total (corpus rate {orr['base_rate']:.0%}); applies after "
          f"{orr['min_shown']:g} shows, at most +-{orr['max_effect']:.0%}; {orr['with_effect']} notes currently affected")
    for label, key in (("boosted", "boosted"), ("demoted", "demoted")):
        for x in orr.get(key, []):
            print(f"    {label} x{x['factor']:.3f}  #{x['id']} [{x['kind']}] shown {x['shown']:g} opened {x['opened']:g}  {x['text']}")
else:
    print("- open-rate prior: no shown/opened data yet (collected as notes are shown and opened)")
