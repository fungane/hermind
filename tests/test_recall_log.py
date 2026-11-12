import pytest
import sys
"""Recall log: which notes a search listed, and which of them the user opened afterwards."""
import json, sqlite3, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _remember(p, text, kw):
    return json.loads(p.handle_tool_call("memory_remember", {"content": text, "kind": "tech", "keywords": kw}))["id"]


def _rows(p, sql):
    return [tuple(r) for r in p._store.conn.execute(sql).fetchall()]


def test_search_logs_listed_ids_and_get_points_back(provider):
    a = _remember(provider, "雲端備份使用 restic 每晚 3 點執行", "備份 restic 雲端 每晚")
    _remember(provider, "網站證書由 certbot 自動續期", "證書 certbot 續期")
    provider.handle_tool_call("memory_search", {"query": "restic 備份"})
    sid, = _rows(provider, "SELECT id FROM usage_log WHERE kind='search'")[-1]
    assert (sid, a, 0) in _rows(provider, "SELECT event_id, note_id, rank FROM recall_shown")
    provider.handle_tool_call("memory_get", {"id": a})
    assert _rows(provider, "SELECT search_id FROM usage_log WHERE kind='get'")[-1][0] == sid


def test_get_of_a_note_nobody_listed_has_no_search_id(provider):
    a = _remember(provider, "unlisted note about quarterly tax filing", "tax filing quarterly")
    provider.handle_tool_call("memory_get", {"id": a})
    assert _rows(provider, "SELECT search_id FROM usage_log WHERE kind='get'")[-1][0] is None


def test_feedback_links_and_automatic_recall_is_logged(provider):
    a = _remember(provider, "資料庫備份保留 14 天並加密", "資料庫 備份 保留 加密")
    assert provider.prefetch("資料庫備份要保留幾多日") != ""
    sid, = _rows(provider, "SELECT id FROM usage_log WHERE kind='search' AND source='auto'")[-1]
    assert (sid, a) in _rows(provider, "SELECT event_id, note_id FROM recall_shown")
    provider.handle_tool_call("memory_feedback", {"id": a, "helpful": True})
    assert _rows(provider, "SELECT search_id FROM usage_log WHERE kind='feedback'")[-1][0] == sid


def test_old_database_gets_the_column_and_logging_still_works(provider):
    con = provider._store.conn
    con.executescript("DROP TABLE IF EXISTS usage_log; DROP TABLE IF EXISTS recall_shown;"
                      "CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,"
                      "source TEXT, query TEXT, n_queries INTEGER, n_results INTEGER, note_id INTEGER, helpful INTEGER);")
    provider._store._log_migrated = False
    eid = provider._store.log_event("search", query="x", shown=[1, 2])
    assert eid and "search_id" in [r[1] for r in con.execute("PRAGMA table_info(usage_log)")]
    assert _rows(provider, "SELECT note_id FROM recall_shown ORDER BY rank") == [(1,), (2,)]
