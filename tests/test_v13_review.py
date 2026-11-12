"""review queue for LLM-extracted notes."""
import json

import pytest


class _FakeLLM:
    capture_extract = True
    rerank_on = False
    last_usage = {}

    def __init__(self, facts):
        self.facts = facts

    def extract_facts(self, user, assistant):
        return self.facts


def _fact(text, aliases=("a", "b")):
    return {"text": text, "kind": "tech", "aliases": list(aliases)}


def _call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


def _extract(p, text):
    p._llm = _FakeLLM([_fact(text)])
    p._extract_and_store("u", "a", "chunk", "s1")


def _row(p, text_part):
    return p._store.conn.execute("SELECT * FROM notes WHERE content LIKE ?", (f"%{text_part}%",)).fetchone()


def test_old_database_gets_review_column_default_active(tmp_path, store_mod):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'chat', keywords TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL,
        source_session TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '',
        doc_len_content INTEGER NOT NULL DEFAULT 0, doc_len_keywords INTEGER NOT NULL DEFAULT 0);
        INSERT INTO notes(content, created_at) VALUES ('legacy note about caddy', 1700000000);
        PRAGMA user_version=4;""")
    c.commit(); c.close()
    s = store_mod.DecisionStore(path)
    assert s.conn.execute("SELECT review FROM notes").fetchone()[0] == "active"
    assert s.pending_count() == 0
    s.close()
    s = store_mod.DecisionStore(path)   # reopening is idempotent
    assert s.count() == 1
    s.close()


def test_extracted_notes_are_pending_manual_notes_active(provider):
    nid = _call(provider, "memory_remember", content="用戶偏好用 Caddy 做反向代理", keywords="caddy proxy")["id"]
    _extract(provider, "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿")
    assert provider._store.get(nid)["review"] == "active"
    assert _row(provider, "swap 設定為")["review"] == "pending"
    assert provider._store.pending_count() == 1


def test_review_queue_flag_off_keeps_old_behaviour(provider):
    provider._config["review_queue"] = False
    _extract(provider, "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿")
    assert _row(provider, "swap 設定為")["review"] == "active"


def test_pending_is_searchable_marked_and_downweighted(provider):
    s = provider._store
    b, _ = s.add("Zebrafish2 實驗室的 incubator 溫度設定為 28 度", kind="tech", keywords="zebrafish incubator",
                 review="pending")
    q = "zebrafish2 incubator 溫度"
    pending = s.search(q, limit=5)[0]
    assert pending["id"] == b and pending["review"] == "pending"      # searchable
    s.confirm(b)
    confirmed = s.search(q, limit=5)[0]
    assert abs(pending["score"] / confirmed["score"] - 0.6) < 0.01     # same note, x0.6 while pending
    s.conn.execute("UPDATE notes SET review='pending' WHERE id=?", (b,))
    out = _call(provider, "memory_search", query=q)
    marked = out["results"][0]
    assert marked["text"].startswith("[pending] ") and marked["pending"] is True
    assert "[pending]" in provider.prefetch(q)
    s.confirm(b)
    assert not _call(provider, "memory_search", query=q)["results"][0]["text"].startswith("[pending]")


def test_pending_ranks_below_an_equal_confirmed_note(provider):
    s = provider._store
    a, _ = s.add("Zebrafish 實驗室的 incubator 溫度設定為 28 度", kind="tech", keywords="zebrafish incubator")
    b, _ = s.add("Zebrafish 養殖室的 incubator 溫度設定為 28 度", kind="tech", keywords="zebrafish incubator",
                 review="pending")
    assert [h["id"] for h in s.search("zebrafish incubator 溫度", limit=5)] == [a, b]


def test_confirm_makes_note_normal(provider):
    _extract(provider, "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿")
    nid = _row(provider, "swap 設定為")["id"]
    listed = _call(provider, "memory_review")
    assert listed["total_pending"] == 1 and listed["pending"][0]["id"] == nid
    res = _call(provider, "memory_review", action="confirm", id=nid)
    assert res["done"] == [nid]
    assert provider._store.get(nid)["review"] == "active"
    assert _call(provider, "memory_review")["count"] == 0
    assert _call(provider, "memory_review", action="confirm", id=nid)["skipped"] == [nid]   # not pending any more


def test_reject_is_soft_forget_and_restorable(provider):
    s = provider._store
    text = "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿"
    _extract(provider, text)
    nid = _row(provider, "swap 設定為")["id"]
    assert _call(provider, "memory_review", action="reject", id=nid)["done"] == [nid]
    # still in the database (restorable), hidden from every read, and not recreated by capture
    assert s.conn.execute("SELECT status FROM notes WHERE id=?", (nid,)).fetchone()[0] == "rejected"
    assert not s.search("swap 2GB", limit=5, include_history=True)
    assert not s.list(include_hidden=False)
    assert s.is_forgotten(text)
    before = s.count()
    _extract(provider, text)
    assert s.count() == before
    assert _call(provider, "memory_review", rejected=True)["rejected"][0]["id"] == nid
    # restore: back in the queue, tombstone lifted
    assert _call(provider, "memory_review", action="restore", id=nid)["done"] == [nid]
    assert not s.is_forgotten(text)
    assert s.get(nid)["status"] == "active" and s.get(nid)["review"] == "pending"
    assert s.search("swap 2GB", limit=3)


def test_only_pending_notes_can_be_rejected(provider):
    nid = _call(provider, "memory_remember", content="用戶偏好用 Caddy 做反向代理", keywords="caddy proxy")["id"]
    assert _call(provider, "memory_review", action="reject", id=nid)["skipped"] == [nid]
    assert provider._store.get(nid)["status"] == "active"


def test_read_only_context_cannot_confirm_but_can_list(provider):
    _extract(provider, "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿")
    nid = _row(provider, "swap 設定為")["id"]
    provider._writable = False
    assert "error" in _call(provider, "memory_review", action="confirm", id=nid)
    assert _call(provider, "memory_review")["total_pending"] == 1


def test_pending_not_merged_into_confirmed_note_and_not_summarised(provider):
    s = provider._store
    a, _ = s.add("Caddy 反向代理設定在 /etc/caddy/Caddyfile 監聽 443 埠", kind="tech")
    b, _ = s.add("Caddy 反向代理設定在 /etc/caddy/Caddyfile 監聽 443 埠並且啟用 gzip", kind="tech", review="pending")
    s.consolidate(full=True)
    assert s.get(a)["status"] == "active" and s.get(b)["status"] == "active"
    s.confirm(b)
    s.consolidate(full=True)
    assert {s.get(a)["status"], s.get(b)["status"]} == {"active", "merged"}


def test_tool_schema_registered(provider):
    names = [t["name"] for t in provider.get_tool_schemas()]
    assert "memory_review" in names


def test_forget_works_on_rejected_note(provider):
    _extract(provider, "VPS 的 swap 設定為 2GB，因為記憶體只有 4GB 而且常常用滿")
    nid = _row(provider, "swap 設定為")["id"]
    provider._store.reject(nid)
    assert provider._store.forget(nid)
    assert provider._store.get(nid) is None
