import json
import sys
from pathlib import Path


def test_interface_and_schemas(provider, pkg):
    assert provider.name == "hermind" and provider.is_available()
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert names == {"memory_remember", "memory_search", "memory_forget", "memory_list", "memory_reflect",
                     "memory_related", "memory_update", "memory_feedback", "memory_get", "memory_review"}
    desc = next(s for s in provider.get_tool_schemas() if s["name"] == "memory_remember")["description"]
    assert "5-10" in desc and "Cantonese" in desc
    assert "progress snapshots" in desc and "memory_update" in desc      # what is not worth saving


def test_db_path_is_profile_scoped(provider, tmp_path):
    assert Path(provider._store.db_path) == tmp_path / "dm.db"


def test_remember_search_forget_list(provider):
    r = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "免費模型下架時，fallback 改用另一個付費模型並通知用戶", "kind": "decision",
        "keywords": "死咗 點算 備用 失效 delisted 後備 怎麼辦"}))
    assert r["created"] and "hint" not in r
    res = json.loads(provider.handle_tool_call("memory_search", {"query": "免費模型死咗點算"}))
    assert res["count"] >= 1 and res["results"][0]["id"] == r["id"]
    lst = json.loads(provider.handle_tool_call("memory_list", {"kind": "decision"}))
    assert lst["count"] == 1
    f = json.loads(provider.handle_tool_call("memory_forget", {"id": r["id"]}))
    assert f["deleted"]
    assert json.loads(provider.handle_tool_call("memory_search", {"query": "免費模型"}))["count"] == 0


def test_remember_hints_when_few_keywords(provider):
    r = json.loads(provider.handle_tool_call("memory_remember", {"content": "用繁體中文回覆", "kind": "preference",
                                                                  "keywords": "繁體"}))
    assert "hint" in r


def test_bad_tool_and_kind(provider):
    assert "error" in json.loads(provider.handle_tool_call("nope", {}))
    assert "error" in json.loads(provider.handle_tool_call("memory_remember", {"content": "x", "kind": "bad",
                                                                                "keywords": ""}))


def test_prefetch_compact_with_ids_and_dates(provider):
    for i in range(7):
        provider.handle_tool_call("memory_remember", {"content": f"VPS 記憶體決定 {i}：升級到 {i} GB",
                                                      "kind": "decision", "keywords": "服務器 RAM"})
    out = provider.prefetch("服務器記憶體夠唔夠")
    lines = [l for l in out.splitlines() if l.startswith("#")]
    assert 3 <= len(lines) <= 5
    assert all("[decision 20" in l for l in lines)
    assert provider.recall_status().count == len(lines)
    assert provider.prefetch("ok") == ""


def test_sync_turn_captures_chat_and_dedups(provider):
    provider.sync_turn("週報幾點出？", "每日 10:30 發送。", session_id="20261018_120000_abcd")
    provider.sync_turn("週報幾點出？", "每日 10:30 發送。", session_id="20261018_120000_abcd")
    notes = provider._store.list(kind="chat")
    assert len(notes) == 1 and notes[0]["content"].startswith("User: 週報幾點出")
    assert notes[0]["expires_at"] is not None


def test_sync_turn_truncates(provider):
    provider.sync_turn("問" * 1000, "答" * 5000)
    assert len(provider._store.list(kind="chat")[0]["content"]) <= 620


def test_cron_session_is_skipped(pkg, tmp_path):
    for kwargs, sid in (({"agent_context": "cron"}, "s1"), ({"platform": "cron"}, "s2"), ({}, "cron_daily_123")):
        p = pkg.HermindProvider(config={"db_path": str(tmp_path / f"{sid}.db")})
        p.initialize(sid, hermes_home=str(tmp_path), **kwargs)
        p.sync_turn("今日新聞摘要", "內容……")
        p.on_session_end([{"role": "user", "content": "今日新聞"}, {"role": "assistant", "content": "好"}])
        assert p._store.count() == 0, (kwargs, sid)
        p.shutdown()


def test_skip_rule_configurable(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": str(tmp_path / "x.db"), "skip_sources": ""})
    p.initialize("cron_job", hermes_home=str(tmp_path), platform="cron")
    p.sync_turn("今日新聞摘要", "內容")
    assert p._store.count() == 1
    p.shutdown()


def test_session_end_captures_last_exchange(provider):
    provider.on_session_end([{"role": "user", "content": "決定用 SQLite 定 Postgres？"},
                             {"role": "assistant", "content": "用 SQLite，夠輕。"}])
    assert "SQLite" in provider._store.list(kind="chat")[0]["content"]


def test_subagent_is_read_only(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": str(tmp_path / "s.db")})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="subagent")
    assert "error" in json.loads(p.handle_tool_call("memory_remember", {"content": "x", "keywords": "y"}))
    p.sync_turn("hello there friend", "reply")
    assert p._store.count() == 0
    p.shutdown()


def test_register(pkg):
    got = []

    class Ctx:
        def register_memory_provider(self, p):
            got.append(p)

    pkg.register(Ctx())
    assert got and got[0].name == "hermind"


def test_importer_classify(pkg):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import import_hindsight as ih
    assert ih.classify({"text": "最後決定用方案 B"}) == "decision"
    assert ih.classify({"text": "據報某公司今日發布新產品"}) == "news"
    assert ih.classify({"text": "目前 VPS 有 4 GB RAM"}) == "state"
    assert ih.classify({"text": "在 config.yaml 設定 provider 並安裝插件"}) == "tech"
    assert ih.classify({"text": "User mentioned Telegram and Hermes yesterday"}) == "chat"  # Latin alone != tech
    assert ih.classify({"text": "用戶偏好繁體中文回覆"}) == "preference"
    assert ih.classify({"text": "用戶今天心情不錯"}) == "chat"


def test_ack_skips_recall_but_not_real_questions(provider, pkg):
    provider.handle_tool_call("memory_remember", {"content": "可以 做吧 服務器記憶體決定", "kind": "decision",
                                                  "keywords": "服務器 記憶體 RAM 升級 決定"})
    for t in ("可以", "做吧", "都幫我做吧", "補吧", "修", "OK！", "可以。"):
        assert pkg.is_ack(t) and provider.prefetch(t) == ""
    assert not pkg.is_ack("可以幫我查下服務器記憶體嗎")
    assert provider.prefetch("服務器記憶體決定") != ""


def test_memory_get_is_logged(provider):
    r = json.loads(provider.handle_tool_call("memory_remember", {"content": "測試 get 記錄", "kind": "tech",
                                                                 "keywords": "get log 記錄 測試 usage"}))
    provider.handle_tool_call("memory_get", {"id": r["id"]})
    assert provider._store.usage_report(1)["gets"] == 1


class _SpyLLM:
    capture_extract = True
    last_usage = {}

    def __init__(self):
        self.seen = []

    def extract_facts(self, user, assistant):
        self.seen.append(("facts", user, assistant))
        return []

    def extract_candidates(self, transcript):
        self.seen.append(("cands", transcript))
        return []


def test_secret_late_in_long_message_never_reaches_llm(provider):
    """The stored chunk is truncated, but the extractor gets the whole turn: a key beyond the cut must still block it."""
    spy = _SpyLLM()
    provider._llm = spy
    key = "sk-" + "a1B2c3D4e5" * 4
    long_user = "請幫我設定這個服務。" + "背景說明。" * 200 + f" API key is {key}"
    provider._capture(long_user, "好的")
    provider._capture("普通訊息，沒有機密", "助手回覆 " + "內容。" * 300 + f" token: {key}")
    if provider._pool is not None:
        provider._pool.shutdown(wait=True)
    assert spy.seen == []


def test_precompress_drops_secret_messages_from_transcript(provider):
    spy = _SpyLLM()
    provider._llm = spy
    key = "sk-" + "Z9y8X7w6V5" * 4
    provider.on_pre_compress([
        {"role": "user", "content": "我決定用 SQLite 做記憶庫"},
        {"role": "user", "content": "x" * 900 + f" password is {key}"},
    ])
    sent = " ".join(str(x) for call in spy.seen for x in call)
    assert key not in sent and "SQLite" in sent


def test_empty_results_tell_the_model_not_to_loop(provider):
    r = json.loads(provider.handle_tool_call("memory_search", {"query": "完全無關的詞語 zzzqqq"}))
    assert r["count"] == 0 and "Do not repeat" in r["hint"]
    r = json.loads(provider.handle_tool_call("memory_related", {"entity": "不存在的實體xyz"}))
    assert r["found"] is False and "hint" in r
    assert "Do not guess" in provider.handle_tool_call("memory_get", {"id": 99999})


def test_related_timeline_shows_old_values_oldest_first(provider):
    st = provider._store
    a, _ = st.add("User's VPS swap is 2GB", kind="state", created_at=1_000_000.0, entities=["VPS"])
    b, _ = st.add("User's VPS swap is now 8GB", kind="state", created_at=1_100_000.0, entities=["VPS"])
    st.mark_stale()
    r = json.loads(provider.handle_tool_call("memory_related", {"entity": "VPS", "mode": "timeline"}))
    assert r["mode"] == "timeline" and [n["id"] for n in r["notes"]] == [a, b]
    assert [n["current"] for n in r["notes"]] == [False, True]
    assert r["notes"][0]["outdated_by"] == b
    recent = json.loads(provider.handle_tool_call("memory_related", {"entity": "VPS"}))
    assert [n["id"] for n in recent["notes"]] == [b, a] and "mode" not in recent


def test_legacy_config_section_is_still_read(pkg, tmp_path, monkeypatch):
    import types
    cfg = {"plugins": {"decision-memory": {"prefetch_limit": 2}}}
    fake = types.ModuleType("hermes_cli.config")
    fake.load_config_readonly = lambda: cfg
    fake.cfg_get = lambda c, *keys, default=None: (lambda f: f(f, c, keys))(
        lambda f, c, k: c if not k else (f(f, c.get(k[0]), k[1:]) if isinstance(c, dict) and k[0] in c else default))
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
    assert pkg._load_plugin_config() == {"prefetch_limit": 2}
    cfg["plugins"]["hermind"] = {"prefetch_limit": 5}
    assert pkg._load_plugin_config() == {"prefetch_limit": 5}


def test_old_context_tag_is_still_stripped():
    from hermind.screen import strip_injected
    old = "hi\n<decision-memory-context>\n#1 old note\n</decision-memory-context>\nbye"
    new = "hi\n<hermind-context>\n#1 new note\n</hermind-context>\nbye"
    assert "note" not in strip_injected(old) and "note" not in strip_injected(new)
