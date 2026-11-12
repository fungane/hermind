"""bi-temporal validity (valid_from / valid_to) and as_of queries."""
import json
import sqlite3
import time

import pytest

DAY = 86400.0


def ts(y, m, d, h=12):
    return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))


def day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


def test_old_database_gets_valid_from_column(tmp_path, store_mod):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'chat', keywords TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL,
        source_session TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '',
        doc_len_content INTEGER NOT NULL DEFAULT 0, doc_len_keywords INTEGER NOT NULL DEFAULT 0, valid_to REAL);
        INSERT INTO notes(content, created_at, valid_to) VALUES ('legacy note about caddy', 1700000000, 1700500000);
        PRAGMA user_version=3;""")
    c.commit(); c.close()
    s = store_mod.DecisionStore(path)
    row = s.get(1)
    assert row["valid_from"] is None and row["valid_to"] == 1700500000      # existing data untouched
    s.close()


def test_add_validity_and_validation(store):
    nid, _ = store.add("Zorblax 訂閱方案由 2026-09-01 生效至 2026-12-31", kind="state", valid_from="2026-09-01",
                       valid_to="2026-12-31")
    r = store.get(nid)
    assert day(r["valid_from"]) == "2026-09-01" and day(r["valid_to"]) == "2026-12-31"
    with pytest.raises(ValueError):
        store.add("another Zorblax fact", valid_from="2026-12-31", valid_to="2026-09-01")
    with pytest.raises(ValueError):
        store.add("third Zorblax fact", valid_from="not a date")


def test_as_of_window_is_inclusive_from_exclusive_to(store):
    nid, _ = store.add("Zorblax 訂閱方案生效", kind="state", valid_from="2026-09-01", valid_to="2026-12-31",
                       created_at=ts(2026, 8, 1))
    hit = lambda d: [h["id"] for h in store.search("Zorblax 訂閱", as_of=d, now=ts(2027, 1, 10))]  # noqa: E731
    assert hit("2026-08-31") == []                 # before valid_from
    assert hit("2026-09-01") == [nid]              # valid_from day is inside
    assert hit("2026-12-30") == [nid]
    assert hit("2026-12-31") == []                 # valid_to day is outside
    assert hit("2027-01-05") == []


def test_null_valid_from_falls_back_to_created_date(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech", created_at=ts(2026, 10, 10))
    q = lambda d: [h["id"] for h in store.search("Quuxlet port", as_of=d, now=ts(2026, 11, 20))]  # noqa: E731
    assert q("2026-10-09") == [] and q("2026-10-10") == [nid] and q("2026-11-01") == [nid]


def test_update_sets_validity_boundaries_and_as_of_finds_both_versions(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech", created_at=ts(2026, 9, 1))
    t_upd = ts(2026, 10, 1)
    assert store.update(nid, "Quuxlet 預設 port 改為 9090", now=t_upd)
    live = store.get(nid)
    assert live["valid_from"] == t_upd and live["valid_to"] is None
    hist = store.history(nid)[0]
    assert hist["valid_to"] == t_upd and hist["content"].endswith("8080")
    now = ts(2026, 11, 20)
    texts = lambda d: [h["content"] for h in store.search("Quuxlet port", as_of=d, now=now)]  # noqa: E731
    assert texts("2026-09-15") == ["Quuxlet 預設 port 為 8080"]
    assert texts("2026-10-15") == ["Quuxlet 預設 port 改為 9090"]
    assert texts("2026-08-15") == []
    # without as_of the superseded version stays hidden
    assert [h["content"] for h in store.search("Quuxlet port", now=now)] == ["Quuxlet 預設 port 改為 9090"]


def test_update_keeps_an_explicit_valid_from(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech", valid_from="2026-01-01", created_at=ts(2026, 9, 1))
    store.update(nid, "Quuxlet 預設 port 改為 9090", now=ts(2026, 10, 1))
    assert day(store.get(nid)["valid_from"]) == "2026-01-01"


def test_consolidation_supersede_sets_validity_and_restore_undoes_it(store):
    t_old, t_new = ts(2026, 9, 1), ts(2026, 10, 1)
    old, _ = store.add("目前 Zephyrbox 硬碟剩 13GB，記憶體剩 800MB", kind="state", created_at=t_old)
    new, _ = store.add("目前 Zephyrbox 硬碟剩 21GB，記憶體剩 1.2GB", kind="state", created_at=t_new)
    assert store.consolidate(now=ts(2026, 10, 2))["superseded"] == 1
    assert store.get(old)["valid_to"] == t_new and store.get(new)["valid_from"] == t_new
    now = ts(2026, 11, 1)
    ids = lambda d: [h["id"] for h in store.search("Zephyrbox 硬碟剩幾多", as_of=d, now=now)]  # noqa: E731
    assert ids("2026-09-15") == [old]
    assert ids("2026-10-15") == [new]
    assert [h["id"] for h in store.search("Zephyrbox 硬碟剩幾多", now=now)] == [new]
    store.restore(old)
    assert store.get(old)["valid_to"] is None


def test_merged_duplicates_do_not_appear_as_of(store):
    t = ts(2026, 9, 1)
    a, _ = store.add("Caddy 反向代理設定在 /etc/caddy/Caddyfile 監聽 443 埠", kind="tech", created_at=t)
    b, _ = store.add("Caddy 反向代理設定在 /etc/caddy/Caddyfile 監聽 443 埠並且啟用 gzip", kind="tech", created_at=t + 3600)
    store.consolidate(now=ts(2026, 9, 2))
    got = [h["id"] for h in store.search("Caddy 反向代理", as_of="2026-09-20", now=ts(2026, 11, 1))]
    assert len(got) == 1


def test_memory_remember_search_and_bad_date_via_tools(provider):
    r = _call(provider, "memory_remember", content="Zorblax 訂閱方案 2026 年下半年有效", kind="state",
              keywords="zorblax subscription", valid_from="2026-07-01", valid_to="2027-01-01")
    got = provider._store.get(r["id"])
    assert day(got["valid_from"]) == "2026-07-01" and day(got["valid_to"]) == "2027-01-01"
    inside = _call(provider, "memory_search", query="zorblax subscription", as_of="2026-08-15")
    assert [x["id"] for x in inside["results"]] == [r["id"]]
    outside = _call(provider, "memory_search", query="zorblax subscription", as_of="2027-02-01")
    assert outside["count"] == 0
    assert "error" in _call(provider, "memory_search", query="zorblax", as_of="soon")
    assert "error" in _call(provider, "memory_remember", content="Zorblax x", keywords="a b c d e",
                            valid_from="2027-01-01", valid_to="2026-01-01")
    full = _call(provider, "memory_get", id=r["id"])
    assert full["valid_from"] == "2026-07-01" and full["valid_to"] == "2027-01-01"


def test_timeline_shows_validity_window_and_as_of(provider):
    s = provider._store
    t_old, t_new = ts(2026, 9, 1), ts(2026, 10, 1)
    old, _ = s.add("目前 Zephyrbox 硬碟剩 13GB，記憶體剩 800MB", kind="state", created_at=t_old, entities=["Zephyrbox"])
    new, _ = s.add("目前 Zephyrbox 硬碟剩 21GB，記憶體剩 1.2GB", kind="state", created_at=t_new, entities=["Zephyrbox"])
    s.consolidate(now=ts(2026, 10, 2))
    tl = _call(provider, "memory_related", entity="Zephyrbox", mode="timeline")
    by = {n["id"]: n for n in tl["notes"]}
    assert by[old]["valid"] == {"from": "2026-09-01", "to": "2026-10-01"} and by[old]["current"] is False
    assert by[new]["valid"] == {"from": "2026-10-01", "to": None} and by[new]["current"] is True
    past = _call(provider, "memory_related", entity="Zephyrbox", as_of="2026-09-15")
    assert [n["id"] for n in past["notes"]] == [old] and past["mode"] == "timeline" and past["as_of"] == "2026-09-15"
    assert [n["id"] for n in _call(provider, "memory_related", entity="Zephyrbox", as_of="2026-10-20")["notes"]] == [new]


def test_search_multi_accepts_as_of(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech", created_at=ts(2026, 9, 1))
    store.update(nid, "Quuxlet 預設 port 改為 9090", now=ts(2026, 10, 1))
    got = store.search_multi(["Quuxlet port", "Quuxlet 連接埠"], as_of="2026-09-15", now=ts(2026, 11, 1))
    assert [h["content"] for h in got] == ["Quuxlet 預設 port 為 8080"]


def test_rejected_notes_never_surface_even_with_history_or_as_of(store):
    nid, _ = store.add("Vorplex 排程每日 03:00 執行", kind="tech", review="pending", created_at=ts(2026, 9, 1))
    store.reject(nid)
    now = ts(2026, 11, 1)
    assert store.search("Vorplex 排程", include_history=True, now=now) == []
    assert store.search("Vorplex 排程", as_of="2026-10-01", now=now) == []


def test_times_ending_in_z_or_an_offset_are_read_as_that_time_not_as_local_time():
    """A Z (UTC) or +HH:MM ending names one instant, whatever the machine's time zone; a bare time stays local."""
    import calendar
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermind"))
    from store_common import parse_valid
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    utc = calendar.timegm((2026, 3, 1, 12, 0, 0))
    old = os.environ.get("TZ")
    try:
        for tz in ("Asia/Hong_Kong", "America/Los_Angeles", "UTC"):
            os.environ["TZ"] = tz
            time.tzset()
            assert parse_valid("2026-03-01T12:00:00Z") == utc, tz
            assert parse_valid("2026-03-01T12:00:00.250z") == utc + 0.25, tz
            assert parse_valid("2026-03-01T20:00:00+08:00") == utc, tz
            assert parse_valid("2026-03-01Z") == calendar.timegm((2026, 3, 1, 0, 0, 0)), tz   # a date with Z: UTC midnight
            assert parse_valid("2026-03-01T12:00:00") == time.mktime((2026, 3, 1, 12, 0, 0, 0, 0, -1)), tz
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
