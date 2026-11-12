"""v0.3: injected-block stripping, secret screen, tombstones, trust feedback, write-time check / update,
rank-space boost, per-arm cap and interleave."""

import json

import pytest


# -- 1. injected context ----------------------------------------------------------------------------------------

def test_prefetch_is_wrapped_and_not_recaptured(provider):
    provider.handle_tool_call("memory_remember", {"content": "部署用 Docker Compose，唔用 Kubernetes", "kind": "decision",
                                                  "keywords": "部署 容器 k8s compose 編排 deploy"})
    block = provider.prefetch("部署用咩")
    assert block.startswith("<hermind-context>") and block.endswith("</hermind-context>")
    before = provider._store.count()
    provider.sync_turn(block + "\n", "好的", session_id="")
    assert provider._store.count() == before  # nothing left after stripping -> nothing stored
    provider.sync_turn(block + "\n下星期幾時開會？", "星期三下午。")
    rows = provider._store.list(kind="chat")
    assert rows and all("Related past notes" not in r["content"] and "Docker" not in r["content"] for r in rows)


def test_strip_injected_other_tags_and_data_uris(pkg):
    import sys
    strip = sys.modules["hermind.screen"].strip_injected
    t = ("<memory-context>old note</memory-context>問題 ![img](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAE) "
         "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD")
    out = strip(t)
    assert "old note" not in out and "base64" not in out and "問題" in out


# -- 2. secret screen -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "VPS root password is Xk29sLp0q",
    "OpenAI key sk-proj-abcdefghijklmnopqrstuvwxyz123456",  # public-check: ok (fake secret)
    "AWS AKIAABCDEFGHIJKLMNOP for backups",  # public-check: ok (fake secret)
    "-----BEGIN OPENSSH PRIVATE KEY----- abc",  # public-check: ok (fake secret for the screen test)
    "api_key = 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",
    "Telegram 密碼係 Abc12345!",
])
def test_secret_refused(store, store_mod, text):
    with pytest.raises(store_mod.RefusedWrite):
        store.add(text, kind="tech")


@pytest.mark.parametrize("text", [
    "API key 放喺 .env 嘅 OPENAI_API_KEY，唔好寫入 config",
    "password is required for sudo",
    "token budget raised to 1200 per turn",
    "用 1Password 管理密碼",
])
def test_secret_screen_is_conservative(store, text):
    _, created = store.add(text, kind="tech")
    assert created


def test_remember_secret_returns_clear_message_and_capture_skips(provider):
    r = json.loads(provider.handle_tool_call("memory_remember", {"content": "github token ghp_" + "a1" * 18,
                                                                  "keywords": "github token"}))
    assert r.get("refused") and "secret" in r["error"]
    provider.sync_turn("my password is Hunter2xyz9", "noted")
    assert provider._store.count() == 0


# -- 3. tombstones ----------------------------------------------------------------------------------------------

def test_forgotten_note_is_not_recreated(provider):
    provider.sync_turn("我哋講過搬去新加坡機房", "係，下個月搬。")
    nid = provider._store.list(kind="chat")[0]["id"]
    assert json.loads(provider.handle_tool_call("memory_forget", {"id": nid}))["deleted"]
    provider.sync_turn("我哋講過搬去新加坡機房", "係，下個月搬。")
    assert provider._store.count() == 0
    r = json.loads(provider.handle_tool_call("memory_remember", {"content": "User: 我哋講過搬去新加坡機房\nAssistant: 係，下個月搬。",
                                                                  "keywords": "a b c d e"}))
    assert r.get("refused")


def test_forget_takes_merged_duplicates_along(store):
    a, _ = store.add("Hermes gateway restarts nightly at 03:30 via launchd on the office iMac", kind="tech")
    b, _ = store.add("Hermes gateway restarts nightly at 03:30 via launchd on the office iMac mini", kind="tech")
    store.consolidate()
    keep = a if store.get(a)["status"] == "active" else b
    drop = b if keep == a else a
    assert store.get(drop)["status"] == "merged"
    store.forget(keep)
    assert store.get(drop) is None and store.count() == 0
    assert store.is_forgotten("Hermes gateway restarts nightly at 03:30 via launchd on the office iMac")


# -- 4. trust + feedback ----------------------------------------------------------------------------------------

def test_feedback_clamps_and_hides(store):
    a, _ = store.add("備份用 restic 每日推去 B2", kind="decision", keywords="backup 備份")
    b, _ = store.add("備份用 rsync 每晚推去 NAS", kind="decision", keywords="backup 備份")
    assert store.feedback(a, False) == 0.3
    assert store.feedback(a, False) == pytest.approx(0.1)
    assert store.feedback(a, False) == 0.0
    ids = [h["id"] for h in store.search("備份 backup")]
    assert a not in ids and b in ids
    assert a in [h["id"] for h in store.search("備份 backup", include_low_trust=True)]
    for _ in range(12):
        t = store.feedback(b, True)
    assert t == 1.0 and store.feedback(999, True) is None


def test_positive_trust_breaks_ties(store):
    a, _ = store.add("Mac 嘅 gateway 用 systemd 自動重啟", kind="tech")
    b, _ = store.add("Mac 嘅 gateway 用 cron 自動重啟", kind="tech")
    first = store.search("gateway 自動重啟")[0]["id"]
    other = b if first == a else a
    for _ in range(4):
        store.feedback(other, True)
    assert store.search("gateway 自動重啟")[0]["id"] == other


def test_feedback_tool(provider):
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": "用繁體中文回覆", "kind": "preference",
                                                                    "keywords": "繁體 中文 語言 language reply"}))["id"]
    r = json.loads(provider.handle_tool_call("memory_feedback", {"id": nid, "helpful": False}))
    assert r == {"id": nid, "trust": 0.3, "hidden": False}
    assert "error" in json.loads(provider.handle_tool_call("memory_feedback", {"id": 12345, "helpful": True}))


# -- 5. write-time check + update -------------------------------------------------------------------------------

def test_remember_reports_similar_and_conflicts(provider):
    r1 = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "VPS 有 4GB RAM，用 Ubuntu 22.04", "kind": "state", "keywords": "伺服器 記憶體 主機 server ram",
        "entities": ["VPS"]}))
    r2 = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "VPS 有 8GB RAM，用 Ubuntu 22.04", "kind": "state", "keywords": "伺服器 記憶體 主機 server ram",
        "entities": ["VPS"]}))
    assert any(c["id"] == r1["id"] for c in r2.get("possible_conflicts", []))
    r3 = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "VPS 有 8GB RAM，用 Ubuntu 22.04 LTS", "kind": "state", "keywords": "伺服器 記憶體 主機 server ram",
        "entities": ["VPS"]}))
    assert any(c["id"] == r2["id"] for c in r3.get("similar", []))
    assert "memory_update" in r3["note"]


def test_negation_conflict(store):
    store.add("Gateway 用 Docker 部署", kind="decision", entities=["Docker"])
    chk = store.write_check("Gateway 唔再用 Docker 部署", ["Docker"])
    assert chk["possible_conflicts"] and chk["possible_conflicts"][0]["why"] == "negation differs"


def test_update_keeps_history(provider, store_mod):
    nid = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "每晚 3 點備份去 NAS", "kind": "decision", "keywords": "備份 backup 夜晚 nas 排程"}))["id"]
    r = json.loads(provider.handle_tool_call("memory_update", {"id": nid, "content": "每晚 5 點備份去 B2"}))
    assert r["updated"]
    s = provider._store
    assert s.get(nid)["content"] == "每晚 5 點備份去 B2"
    hist = s.history(nid)
    assert hist and hist[0]["rel"] == "version_of" and hist[0]["content"] == "每晚 3 點備份去 NAS"
    assert hist[0]["valid_to"] is not None and hist[0]["status"] == "superseded"
    hits = s.search("備份 B2 NAS")
    assert [h["id"] for h in hits] == [nid]  # old text is hidden but credits the note
    assert s.search("備份", include_history=True)
    # forgetting the note also forgets its old versions
    s.forget(nid)
    assert s.count() == 0
    assert not s.update(nid, "x")


# -- 11/12. fusion ----------------------------------------------------------------------------------------------

def test_rank_space_boost(store_mod):
    rrf = store_mod.DecisionStore._rrf
    lists = {"bm25": {1: 3.0, 2: 2.0, 3: 1.0}, "entity": {3: 5.0}}
    W = {"bm25": 1.0, "entity": 0.35}
    assert rrf(lists, W, 1.0)[0][0] == 1
    boosted = rrf(lists, W, 1.0, {"entity": "high"})
    assert boosted[0][0] == 3
    # a boosted arm's contribution is bounded by 1/k (never a lexicographic sort on that arm)
    assert boosted[0][1] < 2 / 1.0


def test_interleave_and_cap(store_mod, store):
    inter = store_mod.DecisionStore._interleave([{1: 3, 2: 2, 3: 1}, {9: 5, 1: 4}])
    assert [i for i, _ in inter] == [1, 9, 2, 3]
    for i in range(30):
        store.add(f"記錄 {i}：VPS 監控 cron 第 {i} 項", kind="tech")
    capped = store.search("VPS 監控", limit=20, weights={"cap_per_source": 5})
    assert 0 < len(capped) <= 20
    assert store.dedup_candidates("記錄 3：VPS 監控 cron 第 3 項")


def test_migration_from_v02(store_mod, tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    s = store_mod.DecisionStore(str(db))
    s.add("舊筆記 VPS 4GB", kind="state")
    s.close()
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    s = store_mod.DecisionStore(str(db))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    assert s.search("VPS")[0]["trust"] == 0.5
    s.close()


# -- 6. tiered output -------------------------------------------------------------------------------------------

def test_tiered_search_get_and_prefetch_budget(provider):
    long = "決定 VPS 備份改用 restic，每日凌晨三點推去 Backblaze B2，保留 30 日快照；原因係 rsync 冇版本，" * 6
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": long, "kind": "decision",
                                                                    "keywords": "backup 備份 快照 snapshot 還原"}))["id"]
    brief = json.loads(provider.handle_tool_call("memory_search", {"query": "備份 restic"}))["results"][0]
    assert set(brief) == {"id", "kind", "date", "text"} and len(brief["text"]) <= 60
    more = json.loads(provider.handle_tool_call("memory_search", {"query": "備份 restic", "detail": "more"}))["results"][0]
    assert len(more["content"]) <= 200 and more["truncated"] and "keywords" in more
    full = json.loads(provider.handle_tool_call("memory_get", {"id": nid}))
    assert full["content"] == long.strip()
    for i in range(6):
        provider.handle_tool_call("memory_remember", {"content": f"備份第 {i} 個方案：restic 加 B2 {i}", "kind": "decision",
                                                      "keywords": "backup 備份 方案 plan 還原"})
    provider._config["prefetch_chars"] = 150
    block = provider.prefetch("備份 restic 方案")
    body = block.split("\n")[2:-1]
    assert len(body) == 1 or sum(len(x) + 1 for x in body) <= 150
    assert all(len(x) < 90 for x in body)


# -- 7. pre-compress + mirror -----------------------------------------------------------------------------------

def test_on_pre_compress_picks_candidates(provider):
    msgs = [{"role": "user", "content": "部 VPS 得 4GB RAM，跑唔跑到？"},
            {"role": "assistant", "content": "可以。結論：改用 llama.cpp 量化模型，唔用 vLLM。今日天氣好好。"},
            {"role": "user", "content": "<hermind-context>#1 決定 xxx</hermind-context>好"},
            {"role": "tool", "content": "決定 ignored tool output"}]
    out = provider.on_pre_compress(msgs)
    assert "改用 llama.cpp" in out and "天氣" not in out and "ignored" not in out and "#1" not in out
    rows = provider._store.list(kind="chat")
    assert rows and all(r["candidate"] for r in rows)
    assert provider.on_pre_compress([]) == ""


def test_on_memory_write_mirrors_builtin(provider):
    provider.on_memory_write("add", "user", "User prefers short replies with bullet points")
    n = provider._store.list()[0]
    assert n["kind"] == "preference" and n["pinned"] and n["source_session"] == "builtin:user"
    provider.on_memory_write("replace", "user", "User prefers replies in Cantonese",
                             {"previous_content": "User prefers short replies with bullet points"})
    assert provider._store.get(n["id"])["content"] == "User prefers replies in Cantonese"
    provider.on_memory_write("remove", "user", "", {"previous_content": "User prefers replies in Cantonese"})
    assert provider._store.get(n["id"]) is None
    provider.on_memory_write("add", "memory", "VPS 4GB RAM, Ubuntu 22.04")
    assert provider._store.list()[0]["kind"] == "state"


# -- 10. scope / tags -------------------------------------------------------------------------------------------

def test_scope_and_tags(store_mod, tmp_path):
    db = str(tmp_path / "shared.db")
    a = store_mod.DecisionStore(db, scope="alice")
    b = store_mod.DecisionStore(db, scope="bob")
    a.add("部署用 Docker Compose", kind="decision", tags="infra homelab")
    b.add("部署用 Kubernetes", kind="decision", tags="infra")
    a.add("全家共用 NAS 喺客廳", kind="state", scope="shared")
    assert [h["content"] for h in a.search("部署")] == ["部署用 Docker Compose"]
    assert [h["content"] for h in b.search("部署")] == ["部署用 Kubernetes"]
    assert len(a.search("部署", scope="*")) == 2
    assert b.search("NAS") and a.search("NAS")
    assert a.list(tags="homelab")[0]["content"] == "部署用 Docker Compose"
    assert not b.list(tags="homelab")
    assert a.search("部署", tags="homelab") and not a.search("部署", tags="nothing")
    a.close(), b.close()


def test_provider_scope_from_profile(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": str(tmp_path / "x.db"), "scope": "{profile}"})
    p.initialize("s1", hermes_home=str(tmp_path / "guest"), platform="cli")
    assert p._store.scope == "guest"
    p.shutdown()


# -- 13. graph fan-out ------------------------------------------------------------------------------------------

def test_graph_fanout_caps_hub_entity(store):
    for i in range(60):
        store.add(f"Hermes 第 {i} 項設定：gateway 參數 {i}", kind="tech", entities=["Hermes"])
    store.add("Gateway 搬去 Mac mini，Hermes 用 systemd 管理", kind="decision", entities=["Hermes", "Mac mini"])
    hits = store.search("Mac mini gateway", explain=True, limit=10)
    assert hits and hits[0]["content"].startswith("Gateway 搬去 Mac mini")
    graph = [h for h in store.search("Mac mini", explain=True, limit=60) if "graph" in h["signals"]]
    assert len(graph) <= 30


# -- 14. access tracking / hotness ------------------------------------------------------------------------------

def test_access_tracking_is_batched_and_hot_notes_survive_expiry(store_mod, tmp_path):
    s = store_mod.DecisionStore(str(tmp_path / "a.db"), track_access=True)
    now = 1_800_000_000.0
    nid, _ = s.add("今日傾咗用 restic 做備份", kind="chat", created_at=now - 95 * 86400)
    cold, _ = s.add("今日傾咗晚餐食咩", kind="chat", created_at=now - 95 * 86400)
    s._access_flushed = now - 6 * 86400  # last flush "just now" in the test clock
    s.search("restic 備份", now=now - 6 * 86400)
    assert s.get(nid)["access_count"] == 0  # buffered, not yet written
    s.flush_access(now - 6 * 86400)
    assert s.get(nid)["access_count"] == 1
    assert s.maintenance(now=now, consolidate=False) == 1  # only the cold note is purged
    assert s.get(nid) is not None and s.get(cold) is None
    assert store_mod.hotness(0, 0) == 0.5 and store_mod.hotness(10, 30) < store_mod.hotness(10, 0)
    s.close()


# -- 17. Chinese time vocabulary --------------------------------------------------------------------------------

def test_period_ranges(store_mod):
    import sys
    from datetime import datetime
    tn = sys.modules["hermind.textnorm"]
    now = datetime(2026, 10, 19, 15, 0).timestamp()  # a Monday
    d = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: E731
    for q in ("上個月講過咩", "上月講過咩", "last month", "上个月"):
        w = tn.query_time_window(q, now)
        assert (d(w[0]), d(w[1])) == ("2026-09-01", "2026-10-01"), q
    w = tn.query_time_window("上個禮拜決定咗咩", now)
    assert (d(w[0]), d(w[1])) == ("2026-10-12", "2026-10-19")
    w = tn.query_time_window("今個月用咗幾多", now)
    assert d(w[0]) == "2026-10-01" and w[1] <= now + 1
    w = tn.query_time_window("去年嘅設定", now)
    assert (d(w[0]), d(w[1])) == ("2025-01-01", "2026-01-01")
    w = tn.query_time_window("三日前講過", now)
    assert d(w[0]) <= "2026-10-16" < d(w[1])
    w = tn.query_time_window("兩個星期前", now)
    assert d(w[0]) <= "2026-10-05" < d(w[1])
    assert d(tn.query_time_window("噚日", now)[0]) == "2026-10-18"
    assert tn.query_time_window("部 VPS 幾多 RAM", now) is None


# -- 9. optional LLM layer --------------------------------------------------------------------------------------

def test_llm_off_by_default(provider):
    assert provider._llm is None
    assert "synthesis" not in json.loads(provider.handle_tool_call("memory_reflect", {"query": "x", "synthesize": True}))


def test_llm_failure_falls_back(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": str(tmp_path / "l.db"),
                                           "llm": {"enabled": True, "base_url": "http://127.0.0.1:9", "model": "m",
                                                   "timeout": 1}})
    p.initialize("s", hermes_home=str(tmp_path))
    assert p._llm is not None
    r = json.loads(p.handle_tool_call("memory_remember", {"content": "Gateway 改用 systemd 管理", "keywords": "a b c d e"}))
    assert r["created"]
    out = p.on_pre_compress([{"role": "assistant", "content": "結論：改用 restic 做備份，每日一次。"}])
    assert "restic" in out  # rule-based path
    p.shutdown()


def test_llm_success_path(pkg, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = body["messages"][0]["content"]
            text = ('["SystemD=system daemon"]' if "named entities" in prompt else
                    '["決定用 restic 備份"]' if "conversation excerpt" in prompt else "Use restic (#1).")
            data = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = pkg.HermindProvider(config={"db_path": str(tmp_path / "l.db"), "llm": {
            "enabled": True, "base_url": f"http://127.0.0.1:{srv.server_port}", "model": "m"}})
        p.initialize("s", hermes_home=str(tmp_path))
        nid = json.loads(p.handle_tool_call("memory_remember", {"content": "備份用 restic", "keywords": "a b c d e"}))["id"]
        assert p._store.related("system daemon")["found"]
        assert "restic" in p.on_pre_compress([{"role": "user", "content": "隨便講下"}])
        pack = json.loads(p.handle_tool_call("memory_reflect", {"query": "備份", "synthesize": True}))
        assert pack["synthesis"] == "Use restic (#1)." and nid == 1
        p.shutdown()
    finally:
        srv.shutdown()
