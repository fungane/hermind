"""Import a Hindsight export (tools/export_hindsight.py output) into a hermind DB, build the entity graph and
consolidate.

Only world + experience items are imported (item['text'], item['date'], item['entities'], item['proof_count']).
Kind comes from textnorm.classify_kind (decision / preference / tech / news / chat / state; Latin letters alone do
not make a note "tech"). Keywords are generated from the entities plus synonyms.yaml expansion of the text, since
imported rows have no agent-written aliases. After loading, the graph is re-linked against the final entity
dictionary and consolidate() merges near-duplicates / supersedes outdated state notes (nothing is deleted).

Usage: python import_hindsight.py SRC.json DST.db
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from store import DecisionStore, RefusedWrite  # noqa: E402
from textnorm import classify_kind  # noqa: E402

RE_SESSION_DOC = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")


def classify(item: dict) -> str:
    doc = str(item.get("document_id") or "")
    hint = doc if doc and not RE_SESSION_DOC.match(doc) else ""
    return classify_kind(item.get("text") or "", doc_hint=hint)


def parse_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def item_entities(item: dict) -> list[str]:
    raw = item.get("entities") or ""
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return [str(e).strip() for e in parts if str(e).strip()]


def make_keywords(store: DecisionStore, item: dict) -> str:
    alts = store.synonyms.expand(item.get("text") or "")
    words = []
    for w in item_entities(item) + alts:
        words.extend(w.split())
    return " ".join(dict.fromkeys(words))


def _proof_count(item: dict) -> int:
    try:
        return max(1, min(int(item.get("proof_count") or 1), 1_000_000))
    except (TypeError, ValueError):
        return 1


def _remove_db(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def run(src: str, dst: str, consolidate: bool = True, observations: bool = True) -> dict:
    """Build the DB next to *dst* and swap it in only when the import finished, so a crash (or a malformed export)
    never destroys an existing *dst*."""
    items = json.loads(Path(src).read_text(encoding="utf-8"))
    if isinstance(items, dict):            # raw API page(s): {"items": [...]}
        items = items.get("items") or []
    if not isinstance(items, list):
        raise ValueError("export must be a JSON list (or {'items': [...]})")
    tmp = dst + ".importing"
    _remove_db(tmp)
    t0 = time.time()
    store = DecisionStore(tmp)
    try:
        counts: dict[str, int] = {}
        skipped = dupes = refused = 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            ft = item.get("fact_type")
            if ft not in ("world", "experience", "observation") or not str(item.get("text") or "").strip() \
                    or (ft == "observation" and not observations):
                skipped += 1
                continue
            # Hindsight's consolidated 'observations' become kind='summary' (locked, ranked a little higher): measured
            # +1..2 of 30 questions without hurting anything, so they are imported by default.
            kind = "summary" if ft == "observation" else classify(item)
            try:
                # TTLs (news 14 d, chat 90 d) count from import time: the source dates are history, not freshness.
                nid, created = store.add(str(item["text"]), kind=kind, keywords=make_keywords(store, item),
                                         created_at=parse_date(item.get("date")), expires_at=None,
                                         source_session=str(item.get("document_id") or ""),
                                         entities=item_entities(item), proof_count=_proof_count(item))
            except RefusedWrite:           # credential-like or oversized note: skip it, keep importing
                refused += 1
                continue
            if created and kind == "summary":
                store.conn.execute("UPDATE notes SET locked=1, consolidated_at=? WHERE id=?", (time.time(), nid))
                store.conn.commit()
            if created:
                counts[kind] = counts.get(kind, 0) + 1
            else:
                dupes += 1
        now = time.time()
        with store.conn:
            for kind, days in (("news", 14), ("chat", 90)):
                store.conn.execute("UPDATE notes SET expires_at=? WHERE kind=? AND pinned=0", (now + days * 86400, kind))
        graph = store.rebuild_graph()
        cons = store.consolidate() if consolidate else {}
        out = {"imported": sum(counts.values()), "by_kind": counts, "skipped": skipped, "refused": refused,
               "duplicates": dupes, "graph": graph, "consolidation": cons,
               "active_by_kind": store.stats()["active_by_kind"], "seconds": round(time.time() - t0, 2)}
        store.close()
    except BaseException:
        try:
            store.close()
        finally:
            _remove_db(tmp)               # dst is untouched
        raise
    for suffix in ("-wal", "-shm"):       # stale sidecars of the DB being replaced / leftovers of the temp DB
        for base in (dst, tmp):
            if os.path.exists(base + suffix):
                os.remove(base + suffix)
    os.replace(tmp, dst)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: python import_hindsight.py SRC.json DST.db   (DST.db is overwritten)")
    src, dst = sys.argv[1], sys.argv[2]
    print(json.dumps(run(src, dst), ensure_ascii=False))
