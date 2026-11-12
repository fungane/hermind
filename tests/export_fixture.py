"""A synthetic database that holds every kind of row `hermind export` has to hand over (used by the export tests).

Every text is invented. The ids are returned by name so a test never counts on the order the rows were made in."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import DecisionStore  # noqa: E402

T0 = 1_750_000_000.0          # 2025-06-15: every note is written relative to this, so the fixture is the same on every run
DAY = 86400.0


def build_db(path, *, extra: bool = True, t0: float = T0) -> dict:
    """Make the database at *path* and return {name: note id}. It has:
    edited (a note edited once: its old text is a `superseded` row with a version_of link), merged_keep / merged_drop,
    new_state / old_state (a consolidation supersession), stale_old (a stale_by link), rejected, pending, pinned,
    rich (trust 0.9, tags, both valid dates in the past, scope "work"), plain, and an entity with two aliases."""
    s = DecisionStore(str(path))
    ids: dict = {}
    try:
        ids["edited"], _ = s.add("Priya keeps the Orchid dashboard on PostgreSQL.", kind="tech", keywords="dashboard database",
                                 created_at=t0, entities=["Orchid"])
        s.update(ids["edited"], "Priya keeps the Orchid dashboard on SQLite since March.", now=t0 + 5 * DAY)
        ids["merged_keep"], _ = s.add("Nebula backups for the Marlin rack run every night at two.", kind="tech",
                                      created_at=t0 + DAY)
        ids["merged_drop"], _ = s.add("Nebula backups for the Marlin rack run nightly at two.", kind="tech",
                                      created_at=t0 + 2 * DAY)
        assert s.fold_notes(ids["merged_drop"], ids["merged_keep"], "merged", now=t0 + 3 * DAY)
        ids["old_state"], _ = s.add("The Zephyr gateway listens on port 8080.", kind="state", created_at=t0 + 10 * DAY)
        ids["new_state"], _ = s.add("The Zephyr gateway listens on port 9090.", kind="state", created_at=t0 + 20 * DAY)
        assert s.fold_notes(ids["old_state"], ids["new_state"], "superseded", now=t0 + 21 * DAY)
        ids["stale_old"], _ = s.add("The Falcon server has 4 GB of RAM.", kind="state", created_at=t0 + 30 * DAY)
        ids["stale_new"], _ = s.add("The Falcon server now has 8 GB of RAM.", kind="state", created_at=t0 + 40 * DAY)
        if not s.mark_stale():
            with s.conn:
                s.conn.execute("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,?,?)",
                               (ids["stale_old"], ids["stale_new"], "stale_by", t0 + 41 * DAY))
        ids["rejected"], _ = s.add("Tundra might be the name of the new cluster.", review="pending", created_at=t0 + 50 * DAY)
        assert s.reject(ids["rejected"], now=t0 + 51 * DAY)
        ids["pending"], _ = s.add("Marlin may move to building Delta next year.", review="pending", created_at=t0 + 52 * DAY)
        ids["pinned"], _ = s.add("Always answer in British English.", kind="preference", pinned=True, created_at=t0 + 53 * DAY)
        ids["rich"], _ = s.add("Helios audit reports are kept for seven years.", kind="decision", tags="audit retention",
                               scope="work", valid_from="2024-01-01", valid_to="2024-12-31", created_at=t0 + 54 * DAY,
                               keywords="records retention")
        with s.conn:
            s.conn.execute("UPDATE notes SET trust=0.9 WHERE id=?", (ids["rich"],))
        ids["plain"], _ = s.add("The Quasar gateway was retired after the audit.", kind="chat", created_at=t0 + 55 * DAY)
        if extra:
            s.add_entity_aliases("Orchid", ["orchid dashboard", "ORC"])
    finally:
        s.close()
    return ids


def table_dump(db) -> dict:
    """Every row of every table, sorted: two dumps are equal when nothing in the database changed."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in names:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            out[t] = con.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}").fetchall()
        return out
    finally:
        con.close()
