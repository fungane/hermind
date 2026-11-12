#!/usr/bin/env python3
"""Turn your own recall history into an eval set (read-only; the database is opened with mode=ro).

    python tools/mine_eval.py DB                      # print how many real question -> opened-note pairs exist
    python tools/mine_eval.py DB --out ~/private/real_set.json

A pair is a search or automatic recall (usage_log kind 'search') that listed a note, followed by memory_get or helpful
feedback of that note (search_id points back to the listing). The opened note counts as the right answer for that
question. Needs a database written by 1.0.0 or later with `recall_shown` rows; older rows have no link and are skipped.

The file holds your own questions (up to 200 characters each). It is written with mode 600 and refused inside a git
checkout, so it cannot be committed by accident. Format = tools/recall_eval_set.example.json (`topical` + `negative`).
"""
import argparse, json, os, sqlite3, subprocess, sys
from pathlib import Path


def mine(db: str, min_len: int = 6) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r[1] for r in con.execute("PRAGMA table_info(usage_log)")] if "usage_log" in tables else []
        if "recall_shown" not in tables or "search_id" not in cols:
            return {"pairs": [], "listings": 0, "opened": 0, "ranks": [], "note": "no recall_shown / search_id yet"}
        listings = con.execute("SELECT COUNT(DISTINCT event_id) FROM recall_shown").fetchone()[0]
        rows = con.execute(
            "SELECT s.id, s.query, g.note_id, rs.rank FROM usage_log g JOIN usage_log s ON s.id = g.search_id "
            "JOIN recall_shown rs ON rs.event_id = s.id AND rs.note_id = g.note_id "
            "WHERE g.kind IN ('get', 'feedback') AND COALESCE(g.helpful, 1) = 1 AND g.note_id IS NOT NULL "
            "AND s.query IS NOT NULL AND LENGTH(TRIM(s.query)) >= ? ORDER BY s.id", (min_len,)).fetchall()
    finally:
        con.close()
    by: dict = {}
    for eid, q, nid, rank in rows:
        e = by.setdefault(eid, {"q": q.strip(), "rel": [], "ranks": []})
        if nid not in e["rel"]:
            e["rel"].append(nid)
            e["ranks"].append(rank + 1)
    pairs = list(by.values())
    return {"pairs": pairs, "listings": listings, "opened": len(rows), "ranks": [r for p in pairs for r in p["ranks"]]}


def _in_git_checkout(path: Path) -> bool:
    d = path.parent
    while not d.exists() and d != d.parent:        # the folder may not exist yet: ask about the nearest one that does
        d = d.parent
    try:
        return subprocess.run(["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                              text=True, timeout=10).stdout.strip() == "true"
    except Exception:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db")
    ap.add_argument("--out", help="write the eval set here (mode 600; refused inside a git checkout)")
    a = ap.parse_args(argv)
    r = mine(os.path.expanduser(a.db))
    ranks = r["ranks"]
    print(f"listings logged: {r['listings']}   opened after a listing: {r['opened']}   distinct questions: {len(r['pairs'])}")
    if ranks:
        top = lambda k: sum(1 for x in ranks if x <= k) / len(ranks)
        print(f"rank of the note you opened: top1 {top(1):.0%}  top3 {top(3):.0%}  top5 {top(5):.0%}  (live quality signal)")
    if r.get("note"):
        print(r["note"])
    if a.out:
        out = Path(os.path.expanduser(a.out)).resolve()
        if _in_git_checkout(out):
            print(f"refusing to write {out}: it is inside a git checkout; pick a private folder", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        data = {"_doc": "mined from this database's own history; private", "topical": [[p["q"], p["rel"]] for p in r["pairs"]],
                "negative": []}
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        print(f"wrote {len(r['pairs'])} questions to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
