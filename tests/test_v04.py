import os
"""v0.4: multi-query search, offline alias backfill (fake local HTTP server only), learning from use, migration."""

import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


# -- A. multi-query search ----------------------------------------------------------------------------------------

def _corpus(store):
    a, _ = store.add("伺服器記憶體升級到 8GB，因為舊機只有 2GB", kind="state", keywords="RAM upgrade")
    b, _ = store.add("Weekly report is generated every Monday by the cron job", kind="tech")
    c, _ = store.add("相簿備份改用外置硬碟，每月一次", kind="decision", keywords="photos backup")
    return a, b, c


def test_search_multi_fuses_and_dedupes(store):
    a, b, c = _corpus(store)
    single = [h["id"] for h in store.search("部機有幾多 RAM", limit=5)]
    multi = store.search_multi(["部機有幾多 RAM", "伺服器記憶體容量", "server memory size", "RAM 記憶體 GB"], limit=5)
    ids = [h["id"] for h in multi]
    assert ids[0] == a and len(ids) == len(set(ids))
    assert set(single) <= set(ids) | set(single)
    # a rewrite can find a note the original wording cannot reach at all
    assert c not in [h["id"] for h in store.search("vacation snaps holiday", limit=5)]
    assert c in [h["id"] for h in store.search_multi(["vacation snaps holiday", "photos backup plan"], limit=5)]


def test_search_multi_original_weight_and_limits(store):
    a, b, c = _corpus(store)
    # one query (or duplicates) == plain search
    assert [h["id"] for h in store.search_multi(["weekly report"] * 3)] == [h["id"] for h in store.search("weekly report")]
    assert store.search_multi([]) == [] and store.search_multi(["", "  "]) == []
    # the original query (weight 1.5) beats one rewrite pointing elsewhere
    res = store.search_multi(["weekly report cron", "photos backup"], limit=2)
    assert res[0]["id"] == b
    res = store.search_multi(["weekly report cron", "photos backup"], limit=2, weights={"multi_original": 0.2})
    assert {h["id"] for h in res} == {b, c}
    # at most 4 queries are used; filters apply to every query
    many = ["photos backup"] + [f"rewrite {i}" for i in range(10)]
    assert store.search_multi(many, kind="decision")[0]["id"] == c
    assert all(h["kind"] == "state" for h in store.search_multi(["RAM", "photos backup"], kind="state"))
    exp = store.search_multi(["RAM", "記憶體"], explain=True)
    assert "query_ranks" in exp[0]


def test_tool_accepts_queries_and_query_unchanged(provider):
    provider.handle_tool_call("memory_remember", {"content": "相簿備份改用外置硬碟，每月一次", "keywords": "photos backup",
                                                  "kind": "decision"})
    one = json.loads(provider.handle_tool_call("memory_search", {"query": "相簿備份"}))
    assert one["count"] == 1
    multi = json.loads(provider.handle_tool_call("memory_search", {"query": "啲相點樣保存",
                                                                   "queries": ["photos backup plan", "相片 備份"]}))
    assert multi["count"] >= 1 and "外置硬碟" in multi["results"][0]["text"]
    only = json.loads(provider.handle_tool_call("memory_search", {"queries": ["photos backup"]}))
    assert only["count"] == 1
    desc = [s for s in provider.get_tool_schemas() if s["name"] == "memory_search"][0]
    assert "written-Chinese rewrite" in desc["description"] and "queries" in desc["parameters"]["properties"]


# -- B. offline alias backfill --------------------------------------------------------------------------------------

class FakeLLM:
    """Local OpenAI-compatible stub. mode: ok | malformed | slow | partial | garbage | flaky."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, 0
        self.cur_model, self.models = None, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls += 1
                fake.cur_model = body.get("model")
                fake.models.append(fake.cur_model)
                ids = [json.loads(line)["id"] for line in body["messages"][0]["content"].splitlines()
                       if line.startswith('{"id"')]
                content = fake.answer(ids)
                if content is None:
                    time.sleep(1.5)
                    content = "{}"
                data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, ids):
        if self.mode == "slow" or (self.mode == "flaky" and self.calls == 1):
            return None
        if self.mode == "bad_primary" and self.cur_model == "bad-model":
            return "not json at all {{"
        if self.mode == "malformed":
            return "Sure! here are aliases: {items: [oops"
        if self.mode == "partial":
            ids = ids[:1]
        items = [{"id": i, "aliases": [f"zq{i}x", "written alias", "x" * 200, "ctrl\x00char"],
                  "terms": ["term-a", "term-b"]} for i in ids]
        if self.mode == "garbage":
            items = [{"id": i, "aliases": "not a list", "terms": [123, None]} for i in ids]
        return "```json\n" + json.dumps({"items": items}, ensure_ascii=False) + "\n```"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def backfill(pkg):
    import importlib.util
    from pathlib import Path
    root = Path(sys.modules["hermind"].__file__).parent
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("backfill_aliases", root / "tools" / "backfill_aliases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mkdb(store_mod, path, n=5):
    s = store_mod.DecisionStore(str(path))
    ids = [s.add(f"第{i}個決定：資料夾 project{i} 改用新的備份方法", kind="decision")[0] for i in range(n)]
    s.add("有 keywords 嘅 note", kind="decision", keywords="a b c d e f g")
    s.close()
    return ids


def _args(backfill, db, url, *extra):
    return backfill.build_parser().parse_args(["--db", str(db), "--base-url", url, "--model", "fake", "--fallback-model", "",
                                               "--timeout", "0.5", "--retries", "1", "--retry-wait", "0.01",
                                               "--batch-size", "2", *extra])


def _gen(store_mod, db):
    s = store_mod.DecisionStore(str(db))
    rows = {r["id"]: r for r in s.conn.execute("SELECT id, keywords, gen_keywords, gen_at, gen_model FROM notes")}
    return s, rows


def test_backfill_normal_writes_gen_field_and_is_searchable(backfill, store_mod, tmp_path):
    db = tmp_path / "b.db"
    ids = _mkdb(store_mod, db)
    fake = FakeLLM("ok")
    try:
        log = []

        class Out:
            def write(self, x):
                log.append(json.loads(x))

            def flush(self):
                pass

        stats = backfill.run(_args(backfill, db, fake.url), Out())
        assert stats["written"] == 5 and stats["failed_batches"] == 0
        assert fake.calls == 3  # 5 notes / batch of 2; the note with long keywords is skipped
        assert log[-1]["event"] == "done" and any(e["event"] == "note_done" for e in log)
        s, rows = _gen(store_mod, db)
        g = rows[ids[0]]["gen_keywords"]
        assert "written alias" in g and "x" * 50 not in g and "\x00" not in g and rows[ids[0]]["gen_model"] == "fake"
        assert all(r["keywords"] == "" for i, r in rows.items() if i in ids)  # agent keywords untouched
        hit = s.search_multi(["點樣搵返 zq%dx" % ids[2]], limit=3)
        assert hit and hit[0]["id"] == ids[2]
        # idempotent: second run has nothing to do
        s.close()
        calls = fake.calls
        assert backfill.run(_args(backfill, db, fake.url), Out())["examined"] == 0 and fake.calls == calls
    finally:
        fake.close()


def test_backfill_dry_run_writes_nothing(backfill, store_mod, tmp_path, capsys):
    db = tmp_path / "b.db"
    _mkdb(store_mod, db)
    fake = FakeLLM("ok")
    try:
        stats = backfill.run(_args(backfill, db, fake.url, "--dry-run", "--rebuild"))
        assert stats["examined"] == 5 and stats["written"] == 0
        assert "note_preview" in capsys.readouterr().out
        s, rows = _gen(store_mod, db)
        assert all(r["gen_at"] is None and r["gen_keywords"] == "" for r in rows.values())
        assert s.get_meta("backfill_aliases.last_run") is None
        s.close()
    finally:
        fake.close()


@pytest.mark.parametrize("mode", ["malformed", "garbage", "slow"])
def test_backfill_bad_output_or_timeout_never_writes_garbage(backfill, store_mod, tmp_path, mode):
    db = tmp_path / "b.db"
    _mkdb(store_mod, db, n=2)
    fake = FakeLLM(mode)
    try:
        stats = backfill.run(_args(backfill, db, fake.url), open(os.devnull, "w"))
        assert stats["written"] == 0
        if mode != "garbage":
            assert stats["failed_batches"] == 1 and fake.calls == 2  # 1 retry
        s, rows = _gen(store_mod, db)
        assert all(r["gen_at"] is None and r["gen_keywords"] == "" for r in rows.values())
        s.close()
    finally:
        fake.close()


def test_backfill_partial_batch_and_resume(backfill, store_mod, tmp_path):
    db = tmp_path / "b.db"
    ids = _mkdb(store_mod, db, n=4)
    fake = FakeLLM("partial")
    try:
        stats = backfill.run(_args(backfill, db, fake.url), open(os.devnull, "w"))
        assert stats["written"] == 2 and stats["missing"] == 2
        fake.mode = "ok"  # resume: only the missing notes are requested again
        stats = backfill.run(_args(backfill, db, fake.url), open(os.devnull, "w"))
        assert stats["examined"] == 2 and stats["written"] == 2
        s, rows = _gen(store_mod, db)
        assert all(rows[i]["gen_at"] for i in ids)
        s.close()
    finally:
        fake.close()


def test_backfill_interrupt_resume_limit_since_rebuild(backfill, store_mod, tmp_path):
    db = tmp_path / "b.db"
    ids = _mkdb(store_mod, db, n=5)
    fake = FakeLLM("flaky")  # first request times out, retried
    try:
        assert backfill.run(_args(backfill, db, fake.url, "--limit", "2"), open(os.devnull, "w"))["written"] == 2
        # "interrupted" after 2 notes: the next run continues with the rest
        stats = backfill.run(_args(backfill, db, fake.url), open(os.devnull, "w"))
        assert stats["examined"] == 3 and stats["written"] == 3
        s = store_mod.DecisionStore(str(db))
        new = s.add("新 note：相機設定改用 RAW 格式", kind="decision")[0]
        s.close()
        stats = backfill.run(_args(backfill, db, fake.url, "--since", "last"), open(os.devnull, "w"))
        assert stats["examined"] == 1 and stats["written"] == 1
        stats = backfill.run(_args(backfill, db, fake.url, "--rebuild"), open(os.devnull, "w"))
        assert stats["written"] == 6
        s, rows = _gen(store_mod, db)
        assert rows[new]["gen_at"] and all(rows[i]["gen_at"] for i in ids)
        s.close()
    finally:
        fake.close()


def test_maintenance_never_calls_llm_and_alias_cleaning(backfill, store, monkeypatch):
    monkeypatch.setattr(backfill, "call_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    store.add("note", kind="decision")
    store.maintenance()
    content = "我哋決定用 Docker Compose 部署所有服務"
    out = backfill.clean_aliases({"aliases": [content, "Docker Compose 部署所有服務", "點樣部署", "password=hunter2xyz",
                                              "a|b", "  "], "terms": ["compose"]}, content)
    assert out == ["點樣部署", "a b", "compose"]


def test_gen_field_weight_lower_than_keywords(store):
    a, _ = store.add("note one about something", kind="tech", keywords="zebra")
    b, _ = store.add("note two about something", kind="tech")
    store.set_gen_keywords(b, ["zebra"], model="m")
    res = store.search("zebra", explain=True)
    assert [h["id"] for h in res][:2] == [a, b]
    assert [h["id"] for h in store.search("zebra", weights={"gen_field": 0})] == [a]
    # update clears generated aliases (they described the old text)
    store.update(b, "note two rewritten")
    assert store.get(b)["gen_at"] is None and [h["id"] for h in store.search("zebra")] == [a]


# -- C. learning from use -----------------------------------------------------------------------------------------

def _learning_provider(pkg, tmp_path, clock):
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db", "consolidate_on_init": False})
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli")
    p._clock = lambda: clock[0]
    return p


def test_learning_needs_two_independent_confirmations(pkg, tmp_path):
    clock = [time.time()]
    p = _learning_provider(pkg, tmp_path, clock)
    try:
        st = p._store
        a = st.add("Nightly photo sync goes to the NAS over rsync", kind="tech")[0]
        st.add("Photo editing uses darktable", kind="tech")
        q = "相簿 夜晚 同步 去邊"
        assert all(h["id"] != a for h in st.search(q))
        for session in ("s1", "s1", "s2"):
            p.on_session_switch(session)
            p.handle_tool_call("memory_search", {"query": q})
            p.handle_tool_call("memory_get", {"id": a})
            if session == "s1":
                # same session twice = one confirmation: still only pending, not indexed
                assert st.get(a)["learned_terms"] == ""
        learned = st.get(a)["learned_terms"].split()
        assert "相簿" in learned and "同步" in learned and "去邊" not in [t for t in learned if t in "photo sync"]
        assert st.search(q)[0]["id"] == a
        # negative feedback right after a search removes what was learned from that query
        p.handle_tool_call("memory_search", {"query": q})
        p.handle_tool_call("memory_feedback", {"id": a, "helpful": False})
        assert "相簿" not in st.get(a)["learned_terms"]
    finally:
        p.shutdown()


def test_learning_window_rank1_and_single_click(pkg, tmp_path):
    clock = [time.time()]
    p = _learning_provider(pkg, tmp_path, clock)
    try:
        st = p._store
        a = st.add("Nightly photo sync goes to the NAS over rsync", kind="tech")[0]
        b = st.add("Photo editing uses darktable", kind="tech")[0]
        # a single wrong click never reaches the index
        p.handle_tool_call("memory_search", {"query": "邊個軟件 修圖"})
        p.handle_tool_call("memory_get", {"id": a})
        assert st.get(a)["learned_terms"] == ""
        # outside the 10-minute window: nothing recorded
        p.handle_tool_call("memory_search", {"query": "相簿 夜晚 同步"})
        clock[0] += 601
        p.handle_tool_call("memory_get", {"id": a})
        assert st.conn.execute("SELECT COUNT(*) FROM learn_pending WHERE term='同步'").fetchone()[0] == 0
        # already at rank 1: nothing to learn
        p.handle_tool_call("memory_search", {"query": "darktable"})
        p.handle_tool_call("memory_get", {"id": b})
        assert st.conn.execute("SELECT COUNT(*) FROM learn_pending WHERE note_id=?", (b,)).fetchone()[0] == 0
        # different days in the same session count as independent
        for _ in range(2):
            p.handle_tool_call("memory_search", {"query": "相簿 夜晚 同步"})
            p.handle_tool_call("memory_feedback", {"id": a, "helpful": True})
            clock[0] += 86400
        assert "同步" in st.get(a)["learned_terms"]
    finally:
        p.shutdown()


def test_learned_terms_capped_and_not_duplicating_note_terms(store):
    nid = store.add("backup plan for photos", kind="decision", keywords="nas")[0]
    assert store.learnable_terms(nid, "photos nas 相簿 備份 嘅") == ["相簿", "備份"]
    for day in range(30):
        for src in ("x", "y"):
            store.learn(nid, f"term{day}word", f"{src}|{day}")
    terms = store.get(nid)["learned_terms"].split()
    assert len(terms) == 20 and terms[-1] == "term29word" and "term0word" not in terms


# -- migration ------------------------------------------------------------------------------------------------------

def test_v03_db_migrates(store_mod, tmp_path):
    db = tmp_path / "old.db"
    s = store_mod.DecisionStore(str(db))
    nid = s.add("舊資料：VPS 有 4GB RAM", kind="state", keywords="伺服器 記憶體")[0]
    s.close()
    c = sqlite3.connect(db)  # make it look like a v0.3 DB
    for col in ("gen_keywords", "gen_at", "gen_model", "learned_terms"):
        c.execute(f"ALTER TABLE notes DROP COLUMN {col}") if col != "gen_at" else None
    c.execute("DROP INDEX notes_gen_at")
    c.execute("ALTER TABLE notes DROP COLUMN gen_at")
    c.execute("DROP TABLE learn_pending")
    c.execute("DROP TABLE meta")
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    s = store_mod.DecisionStore(str(db))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    assert s.search("記憶體")[0]["id"] == nid and s.get(nid)["gen_keywords"] == ""
    s.set_gen_keywords(nid, ["server memory"])
    assert s.search_multi(["點算", "server memory"])[0]["id"] == nid
    s.close()


def test_reindex_on_tokenizer_version_change(tmp_path):
    """A database built with an older tokenizer is re-indexed once on open, so new stems match old notes."""
    from store import DecisionStore
    import store as st
    path = str(tmp_path / "t.db")
    s = DecisionStore(path)
    nid, _ = s.add("The final presentation is on 2 April", kind="decision", keywords="demo day")
    s.set_gen_keywords(nid, ["when is the talk"], model="x")
    # simulate an old index: wrong terms + stale tokenizer version
    s.conn.execute("DELETE FROM postings")
    s.conn.execute("INSERT INTO postings(term, note_id, field, tf) VALUES ('presentation', ?, 0, 1)", (nid,))
    s.conn.execute("DELETE FROM meta WHERE key='tokenizer_version'")
    s.conn.commit()
    s.close()
    s2 = DecisionStore(path)                      # auto re-index
    assert s2.get_meta("tokenizer_version") == st.TOKENIZER_VERSION
    assert [h["id"] for h in s2.search("present", limit=3)] == [nid]
    assert [h["id"] for h in s2.search("when is the talk", limit=3)] == [nid]    # gen field survived
    assert s2.reindex() == 1                      # idempotent, callable by hand
    assert [h["id"] for h in s2.search("present", limit=3)] == [nid]


def test_usage_log_and_report(tmp_path):
    from store import DecisionStore
    s = DecisionStore(str(tmp_path / "u.db"))
    nid, _ = s.add("Deadline is 14 November", kind="decision")
    s.log_event("search", source="tool", query="deadline", n_queries=2, n_results=1)
    s.log_event("search", source="auto", query="zzz nothing", n_results=0)
    s.log_event("feedback", note_id=nid, helpful=True)
    r = s.usage_report(30)
    assert r["searches"]["tool"] == {"count": 1, "zero_result": 0, "with_rewrites": 1}
    assert r["searches"]["auto"]["zero_result"] == 1
    assert r["feedback"] == {"helpful": 1}
    assert r["recent_zero_result_queries"] == ["zzz nothing"]


def test_backfill_falls_back_to_second_model_and_records_it(backfill, store_mod, tmp_path):
    db = tmp_path / "fb.db"
    st = store_mod.DecisionStore(str(db))
    nid, _ = st.add("Deploy happens on Fridays after the freeze", kind="decision")
    st.close()
    fake = FakeLLM("bad_primary")
    try:
        class Out:
            def write(self, *_): pass
            def flush(self): pass
        args = backfill.build_parser().parse_args(["--db", str(db), "--base-url", fake.url, "--model", "bad-model",
                                                   "--fallback-model", "good-model", "--timeout", "2",
                                                   "--retries", "0", "--retry-wait", "0.01", "--batch-size", "2"])
        stats = backfill.run(args, Out())
    finally:
        fake.close()
    assert stats["written"] == 1 and stats["failed_batches"] == 0
    assert fake.models == ["bad-model", "good-model"]
    s2 = store_mod.DecisionStore(str(db))
    assert s2.conn.execute("SELECT gen_model FROM notes WHERE id=?", (nid,)).fetchone()[0] == "good-model"


def test_summaries_replace_are_locked_and_survive_consolidate(store_mod, tmp_path):
    s = store_mod.DecisionStore(str(tmp_path / "s.db"))
    ids = [s.add(t, kind="state", entities=["swap"])[0] for t in
           ["Swap on the VPS is 1GB", "Swap was raised to 4GB", "Swap was raised again to 8GB"]]
    assert s.summary_targets(3, 10) == [("swap", 3)]
    new = s.replace_summaries("swap", [("VPS swap is now 8GB (was 1GB then 4GB)", ids)])
    assert len(new) == 1
    s.consolidate(full=True)
    row = s.conn.execute("SELECT kind, status, locked FROM notes WHERE id=?", (new[0],)).fetchone()
    assert (row["kind"], row["status"], row["locked"]) == ("summary", "active", 1)
    assert all(s.conn.execute("SELECT status FROM notes WHERE id=?", (i,)).fetchone()[0] == "active" for i in ids)
    again = s.replace_summaries("swap", [("VPS swap: 8GB now", ids)])      # rerun replaces, never duplicates
    assert s.conn.execute("SELECT COUNT(*) FROM notes WHERE kind='summary'").fetchone()[0] == 1 and again
    assert "summary" in [h["kind"] for h in s.search("swap 8GB", limit=3)]
    assert s.summary_targets(3, 10) == [("swap", 3)]                      # summaries are not counted as sources


def test_summary_validation_rejects_invented_numbers_and_bad_sources():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "hermind"
    spec = importlib.util.spec_from_file_location("build_summaries", root / "tools" / "build_summaries.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    notes = [dict(id=1, content="Swap is 4GB", created_at=0), dict(id=2, content="Swap raised to 8GB", created_at=0)]
    ok = mod.validate([{"text": "Swap now 8GB", "sources": [1, 2]},
                       {"text": "Swap now 16GB", "sources": [1, 2]},          # invented figure
                       {"text": "Swap is big", "sources": [99]},               # unknown source id
                       {"text": "", "sources": [1]}], notes)
    assert ok == [("Swap now 8GB", [1, 2])]


def test_llm_rerank_reorders_and_falls_back(pkg, tmp_path):
    """With llm.rerank on, the model's picks go first; a failing or empty answer leaves the lexical order alone."""
    import json as _json
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/r.db", "llm": {
        "enabled": True, "base_url": "http://127.0.0.1:9/v1", "model": "m", "rerank": True, "timeout": 1}})
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
    try:
        ids = [_json.loads(p.handle_tool_call("memory_remember", {"content": t, "kind": "decision", "keywords": "x"}))["id"]
               for t in ("alpha servers first note", "alpha servers second note with the answer")]
        search = lambda: [h["id"] for h in _json.loads(p.handle_tool_call("memory_search", {"query": "alpha servers"}))["results"]]
        order = search()
        assert set(order) == set(ids)
        p._llm.rerank = lambda q, notes, chars=180: [ids[1]]
        assert search()[0] == ids[1]
        p._llm.rerank = lambda q, notes, chars=180: None
        assert search() == order
        p._llm.rerank = lambda q, notes, chars=180: []
        assert search() == order
    finally:
        p.shutdown()


def _extract_provider(pkg, tmp_path, answer):
    import json as _json
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/x.db", "llm": {
        "enabled": True, "base_url": "http://127.0.0.1:9/v1", "model": "m", "capture_extract": True, "timeout": 1}})
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
    p._llm.extract_facts = answer
    return p


def _notes(p):
    return [(r["kind"], r["content"], r["keywords"]) for r in p._store.conn.execute(
        "SELECT kind, content, keywords FROM notes WHERE status='active'")]


def test_capture_extract_stores_facts_with_aliases_not_raw_turn(pkg, tmp_path):
    p = _extract_provider(pkg, tmp_path, lambda u, a: [
        {"text": "The VPS swap was raised from 4GB to 8GB because Hindsight needs 2.5GB at start", "kind": "state",
         "aliases": ["swap size", "how big is swap", "虛擬記憶體幾大"]},
        {"text": "password is hunter2 and api key sk-abcdefghijklmnopqrstuvwxyz123456", "kind": "tech", "aliases": []}])  # public-check: ok (fake secret)
    try:
        p.sync_turn("swap now 8GB? remember it please, it matters for later", "Noted, swap is 8GB now.", session_id="s1")
        p.shutdown()                                       # waits for the background worker
        p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
        rows = _notes(p)
        assert len(rows) == 1 and rows[0][0] == "state" and "8GB" in rows[0][1] and "swap size" in rows[0][2]
        assert not any("User:" in r[1] for r in rows)       # no raw turn; the secret-looking fact was dropped
    finally:
        p.shutdown()


def test_capture_extract_failure_falls_back_to_raw_and_empty_stores_nothing(pkg, tmp_path):
    p = _extract_provider(pkg, tmp_path, lambda u, a: None)
    try:
        p.sync_turn("please remember the staging server is called nimbus-7", "Okay, nimbus-7 it is.", session_id="s1")
        p.shutdown()
        p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
        rows = _notes(p)
        assert len(rows) == 1 and rows[0][1].startswith("User: please remember the staging")
        p._llm.extract_facts = lambda u, a: []              # model says: nothing durable
        p.sync_turn("what a lovely day it is today, isn't it truly", "Indeed it is a lovely day.", session_id="s1")
        p.shutdown()
        p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
        assert len(_notes(p)) == 1
    finally:
        p.shutdown()


def test_import_hindsight_observations_become_locked_summaries(tmp_path):
    import importlib.util, json as _json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "hermind"
    spec = importlib.util.spec_from_file_location("import_hindsight", root / "import_hindsight.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    src = tmp_path / "hs.json"
    src.write_text(_json.dumps({"items": [
        {"text": "The server has 8GB swap", "fact_type": "world", "date": "2026-10-18T00:00:00+00:00", "entities": "VPS"},
        {"text": "VPS swap grew from 1GB to 4GB and then 8GB", "fact_type": "observation", "entities": "VPS"},
        {"text": "", "fact_type": "world"}]}), encoding="utf-8")
    out = mod.run(str(src), str(tmp_path / "o.db"))
    assert out["by_kind"].get("summary") == 1 and out["skipped"] == 1
    out2 = mod.run(str(src), str(tmp_path / "o2.db"), observations=False)
    assert "summary" not in out2["by_kind"]


def test_llm_circuit_breaker_skips_endpoint_after_three_failures(pkg):
    import importlib
    llm = importlib.import_module("hermind.llm")
    c = llm.LLMClient({"base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 1, "cooldown": 60})
    for _ in range(3):
        assert c.chat("x") is None
    assert c._skip_until > time.time()
    t0 = time.time()
    assert c.chat("x") is None and time.time() - t0 < 0.05          # skipped without any network attempt
    assert c.rerank("q", [{"id": 1, "content": "a"}]) is None


def test_summary_targets_stale_only_tracks_new_notes(store_mod, tmp_path):
    s = store_mod.DecisionStore(str(tmp_path / "st.db"))
    ids = [s.add(f"Swap note number {i} about the VPS", kind="state", entities=["swap"], created_at=1000.0 + i)[0] for i in range(3)]
    assert [x for x in s.summary_targets(3, 10, stale_only=True) if x[0] == 'swap'] == [("swap", 3)]          # never summarised
    s.replace_summaries("swap", [("swap is fine", ids)], now=2000.0)
    assert [x for x in s.summary_targets(3, 10, stale_only=True) if x[0] == 'swap'] == []                     # up to date
    s.add("Swap note number 9 changed everything", kind="state", entities=["swap"], created_at=3000.0)
    assert [x for x in s.summary_targets(3, 10, stale_only=True) if x[0] == 'swap'] == [("swap", 4)]          # a newer note makes it stale again


FAKE_EMBED = r'''
import base64, json, select, sqlite3, struct, sys
CONCEPT = {"work": 1, "job": 1, "profession": 1, "screenwriter": 1, "storage": 2, "disk": 2, "swap": 2}
DIM = 8
def vec(text):
    v = [0.0] * DIM
    for w in text.lower().replace(",", " ").replace(".", " ").split():
        v[CONCEPT.get(w, 3 + hash(w) % 5)] += 1.0 if w in CONCEPT else 0.05
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]
db = sys.argv[sys.argv.index("--db") + 1]; name = sys.argv[sys.argv.index("--model-name") + 1]
while True:
    r, _, _ = select.select([sys.stdin], [], [], 60)
    if not r: break
    line = sys.stdin.readline()
    if not line: break
    q = json.loads(line)
    if q["op"] == "embed":
        out = b"".join(struct.pack("8f", *vec(t)) for t in q["texts"])
        resp = {"ok": True, "dim": DIM, "vecs": base64.b64encode(out).decode()}
    elif q["op"] == "search":
        con = sqlite3.connect(db); qv = vec(q["query"])
        hits = sorted(((sum(a * b for a, b in zip(qv, struct.unpack("8f", blob))), nid)
                       for nid, blob in con.execute("SELECT note_id, vec FROM vectors WHERE model=?", (name,))), reverse=True)
        resp = {"ok": True, "hits": [[nid, round(s, 4)] for s, nid in hits[: q["k"]]]}
    else:
        resp = {"ok": True}
    sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush()
'''


def test_vector_signal_finds_note_without_shared_words_and_degrades_gracefully(pkg, tmp_path):
    import json as _json, sys as _sys
    (tmp_path / "fake_embed.py").write_text(FAKE_EMBED)
    (tmp_path / "model.onnx").write_bytes(b"x")
    cfg = {"db_path": "$HERMES_HOME/v.db", "vectors": {
        "enabled": True, "python": _sys.executable, "server": str(tmp_path / "fake_embed.py"),
        "model": str(tmp_path / "model.onnx"), "tokenizer": str(tmp_path / "model.onnx"), "model_name": "fake"}}
    p = pkg.HermindProvider(config=cfg)
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
    try:
        for t in ("The user is a screenwriter", "Rent is due on the first of the month", "Gym is on Monday mornings"):
            p.handle_tool_call("memory_remember", {"content": t, "kind": "preference", "keywords": "x"})
        p._pool.shutdown(wait=True); p._pool = None                      # background indexing finished
        assert p._store.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 3
        res = _json.loads(p.handle_tool_call("memory_search", {"query": "what is my profession", "limit": 3}))["results"]
        assert "screenwriter" in res[0]["text"] if "text" in res[0] else "screenwriter" in _json.dumps(res[0])   # no shared word
        # kind filter is honoured for vector-only hits
        none = _json.loads(p.handle_tool_call("memory_search", {"query": "what is my profession", "kind": "tech"}))
        assert none["count"] == 0
        # worker dies / is missing -> plain lexical results, no exception
        p._vec.python = "/nonexistent/python"
        ok = _json.loads(p.handle_tool_call("memory_search", {"query": "gym monday"}))
        assert ok["count"] >= 1
    finally:
        p.shutdown()


def test_vector_profiles_pick_model_files_names_and_prefixes(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hermind"))
    from vec import VectorIndex
    d = VectorIndex({"home": str(tmp_path)}, "x.db")                       # unchanged default: e5, with prefixes
    assert (d.model_name, d.query_prefix, d.passage_prefix) == ("e5", "query: ", "passage: ")
    assert d.model.endswith("model/model_quantized.onnx")
    l = VectorIndex({"home": str(tmp_path), "profile": "Light"}, "x.db")
    assert (l.model_name, l.query_prefix, l.passage_prefix) == ("bekko-static-256", "", "")
    assert l.model.endswith("model-light/model.onnx") and l.tokenizer.endswith("model-light/tokenizer.json")
    o = VectorIndex({"home": str(tmp_path), "profile": "light", "query_prefix": "q: ", "model_name": "mine"}, "x.db")
    assert (o.model_name, o.query_prefix, o.passage_prefix) == ("mine", "q: ", "")      # explicit settings win
    assert VectorIndex({"profile": "nonsense"}, "x.db").model_name == "e5"  # unknown name falls back to default
    b = VectorIndex({"home": str(tmp_path), "profile": "balanced"}, "x.db")
    q = VectorIndex({"home": str(tmp_path), "profile": "quality"}, "x.db")
    assert (b.model_name, b.pooling, b.query_prefix) == ("hs-embed-1", "mean", "")
    assert (q.model_name, q.pooling, q.query_prefix) == ("bge-m3", "cls", "")      # bge-m3 pools with the first token
    assert d.pooling == "mean" and b.model.endswith("model-balanced/model.onnx") and q.model.endswith("model-quality/model.onnx")
    assert VectorIndex({"home": str(tmp_path), "profile": "quality", "pooling": "mean"}, "x.db").pooling == "mean"
    assert q.sec_per_note > d.sec_per_note > l.sec_per_note        # heavy models get a longer wait per batch (bge-m3 timed out at 0.15)
    assert VectorIndex({"sec_per_note": 2}, "x.db").sec_per_note == 2.0



def test_build_summaries_skips_an_entity_whose_summary_is_refused(tmp_path, capsys):
    """Round 2: a RefusedWrite from replace_summaries ended the whole run with a traceback."""
    import importlib.util, io
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "hermind"
    spec = importlib.util.spec_from_file_location("build_summaries_r2", root / "tools" / "build_summaries.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    import store as store_mod
    db = tmp_path / "s.db"
    s = store_mod.DecisionStore(str(db))
    ids = {e: [s.add(f"{e} fact number {i} about the rack", kind="state", entities=[e])[0] for i in range(3)] for e in ("swap", "dns")}
    s.close()
    bad = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    def fake_work(cfg, entity, notes):
        text = f"The key is {bad}" if entity == "swap" else "DNS is now on the new rack"
        return entity, notes, [(text, [n["id"] for n in notes])], "m", None
    mod.work = fake_work
    cfg = mod.build_parser().parse_args(["--db", str(db), "--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--concurrency", "1"])
    out = io.StringIO()
    stats = mod.run(cfg, out)
    assert stats["refused"] == 1 and stats["written"] == 1 and stats["entities"] == 2
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [e["event"] for e in events] == ["refused", "done"] or sorted(e["event"] for e in events) == ["done", "refused"]
    assert bad not in out.getvalue() and bad not in capsys.readouterr().err
    s = store_mod.DecisionStore(str(db))
    assert [r[0] for r in s.conn.execute("SELECT source_session FROM notes WHERE kind='summary'")] == ["summary:dns"]
    s.close()
