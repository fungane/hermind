import sys
"""v0.6: soft stale detection for capacity facts (swap / RAM)."""

import pytest

from textnorm import slot_values

DAY = 86400


@pytest.mark.parametrize("text,expected", [
    ("Swap on the build box grew from 4GB to 8GB with an extra swap file", {"swap": 8192}),
    ("The test runner has 2.0Gi swap set in /etc/fstab", {"swap": 2048}),
    ("測試機只有 7.6GB RAM，swap 8GB", {"ram": 7782, "swap": 8192}),
    ("Swap 用了 700MB（总共 4GB）", {}),                       # usage, not capacity
    ("Swap usage 0% 8GB", {}),
    ("7.6GB RAM total with ~1.2GB free", {"ram": 7782}),
    ("RAM 1.3GB available", {}),
    ("預估常駐 RAM 約 25–60MB", {}),                           # estimate range
    ("伺服器的 swap 是兩個 4GB 檔案，合共 8GB", {"swap": 8192}),
    ("swap 4GB, RAM 8GB", {"swap": 4096, "ram": 8192}),
    ("would miss the ~1.3GB RAM budget", {}),
    ("too RAM-heavy for 2GB RAM", {"ram": 2048}),
    ("macOS Spotlight on Disk D was 40GB", {}),               # disk is not tracked
])
def test_slot_values(text, expected):
    assert slot_values(text) == expected


def _states(store):
    old, _ = store.add("User's VPS swap is 2GB", kind="state", created_at=1_000_000.0, entities=["VPS"])
    mid, _ = store.add("User's VPS swap is 4GB, RAM 3.7GB", kind="state", created_at=1_000_000.0 + DAY, entities=["VPS"])
    new, _ = store.add("User's VPS swap is now 8GB", kind="state", created_at=1_000_000.0 + 2 * DAY, entities=["VPS"])
    return old, mid, new


def test_mark_stale_links_older_states(store):
    old, mid, new = _states(store)
    store.mark_stale()
    assert store.stale_ids([old, mid, new]) == {old: new, mid: new}


def test_stale_notes_stay_active_but_rank_lower(store):
    old, mid, new = _states(store)
    store.mark_stale()
    hits = store.search("VPS swap", limit=5, now=1_000_000.0 + 3 * DAY)
    assert hits[0]["id"] == new
    assert {h["id"] for h in hits} >= {old, mid}                  # nothing is hidden
    assert all(h.get("outdated_by") == new for h in hits if h["id"] in (old, mid))


def test_same_value_or_rounding_is_not_stale(store):
    a, _ = store.add("The VPS has 3.7GB RAM", kind="state", created_at=1_000_000.0, entities=["VPS"])
    b, _ = store.add("The VPS has 3.8GB RAM after the resize check", kind="state", created_at=1_000_000.0 + DAY, entities=["VPS"])
    store.mark_stale()
    assert store.stale_ids([a, b]) == {}


def test_consolidate_reports_and_recomputes(store):
    _states(store)
    first = store.consolidate(full=True)
    second = store.consolidate(full=True)
    assert "stale" in first and first["stale"] == second["stale"]   # recomputed each run, not accumulated


def test_idle_decay_demotes_old_chat_but_not_decisions_or_recalled_notes(store):
    t0 = 1_000_000.0
    chat, _ = store.add("Zorblax gadget calibration was discussed", kind="chat", created_at=t0)
    dec, _ = store.add("Zorblax gadget calibration was decided", kind="decision", created_at=t0)
    f = store._idle_factor
    rows = {r["id"]: r for r in store.conn.execute("SELECT * FROM notes")}
    year = t0 + 365 * DAY
    assert f(rows[dec], year) == 1.0
    assert 0.3 <= f(rows[chat], year) < 0.35            # floor, never zero
    assert f(rows[chat], t0 + DAY) > 0.95                # fresh chat is untouched
    recalled = dict(rows[chat]) | {"last_access": year - DAY}
    assert f(recalled, year) > 0.95                      # recalled recently -> keeps its weight
    hits = store.search("Zorblax gadget calibration", limit=5, now=t0 + 80 * DAY)
    assert {h["id"] for h in hits} == {chat, dec} and hits[0]["id"] == dec


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply POSIX file modes")
def test_database_files_are_owner_only(store):
    import os
    import stat
    store.add("private note", kind="tech")
    assert stat.S_IMODE(os.stat(store.db_path).st_mode) == 0o600


def test_opening_current_schema_db_does_not_modify_data(store_mod, tmp_path):
    """Data safety: re-opening an up-to-date database must not rewrite any table (no silent re-index/migration)."""
    import sqlite3
    db = str(tmp_path / "keep.db")
    s = store_mod.DecisionStore(db)
    s.add("決定使用 SQLite 作為記憶庫", kind="decision", keywords="sqlite 資料庫")
    s.close()

    def dump():
        c = sqlite3.connect(db)
        try:
            return {t: c.execute(f'select * from "{t}"').fetchall()
                    for (t,) in c.execute("select name from sqlite_master where type='table'").fetchall()}
        finally:
            c.close()

    before = dump()
    store_mod.DecisionStore(db).close()
    assert dump() == before
