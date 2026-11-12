import json
import sys
from pathlib import Path

import pytest


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


def _expires(provider, nid):
    return provider._store.conn.execute("SELECT expires_at FROM notes WHERE id=?", (nid,)).fetchone()[0]


def test_plugin_remember_auto_or_no_kind_never_expires_and_explicit_chat_news_do(provider):
    import json
    import time
    ids = {}
    for key, args in (("auto", {"kind": "auto"}), ("none", {}), ("chat", {"kind": "chat"}), ("news", {"kind": "news"})):
        out = json.loads(provider.handle_tool_call(
            "memory_remember", {"content": f"Atlas uses PostgreSQL for orders ({key})", **args}))
        ids[key] = out["id"]
    assert _expires(provider, ids["auto"]) is None and _expires(provider, ids["none"]) is None
    assert _expires(provider, ids["chat"]) is not None and _expires(provider, ids["news"]) is not None
    provider._store.maintenance(now=time.time() + 400 * 86400, consolidate=False)
    left = {r[0] for r in provider._store.conn.execute("SELECT id FROM notes")}
    assert left == {ids["auto"], ids["none"]}
    assert provider._store.get(ids["auto"])["kind"] == "chat"               # the classified kind still drives ranking


def test_auto_captured_chat_still_expires(provider):
    provider.sync_turn("Atlas uses PostgreSQL for orders", "Noted, Atlas stores its orders in PostgreSQL.",
                       session_id="20261018_120000_abcd")
    rows = provider._store.conn.execute("SELECT kind, expires_at FROM notes").fetchall()
    assert rows and all(r["kind"] == "chat" and r["expires_at"] is not None for r in rows)


def test_saving_on_purpose_removes_the_expiry_of_an_auto_captured_copy(provider):
    import json
    text = "Atlas uses PostgreSQL for orders"
    nid, _ = provider._store.add(text, kind="chat")
    assert _expires(provider, nid) is not None
    out = json.loads(provider.handle_tool_call("memory_remember", {"content": text, "kind": "auto"}))
    assert out["id"] == nid and _expires(provider, nid) is None


def test_merged_note_that_never_expires_comes_back_when_the_kept_copy_expires(store):
    import time
    long, _ = store.add("Atlas uses PostgreSQL for the orders database and the team reviews the schema every Monday morning",
                        kind="chat")
    short, _ = store.add("Atlas uses PostgreSQL for the orders database and the team reviews the schema every Monday",
                         kind="chat", never_expires=True)
    assert store.consolidate(full=True)["merged"] == 1
    assert store.get(short)["status"] == "merged" or store.get(long)["status"] == "merged"
    store.maintenance(now=time.time() + 400 * 86400, consolidate=False)
    live = store.conn.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0]
    assert live == 1


@pytest.mark.parametrize("tool,args", [
    ("memory_get", {"id": 2 ** 70}), ("memory_update", {"id": 2 ** 70, "content": "x"}), ("memory_forget", {"id": -2 ** 70}),
    ("memory_feedback", {"id": 2 ** 63, "helpful": True}), ("memory_list", {"limit": 2 ** 70}),
    ("memory_review", {"action": "confirm", "ids": [1, 2 ** 70]})])
def test_plugin_integer_arguments_outside_the_sqlite_range_give_a_clear_error(provider, tool, args):
    import json
    out = json.loads(provider.handle_tool_call(tool, args))
    assert "out of range" in out["error"] and "Overflow" not in out["error"]


ODD = [5, 2.5, "str", [1], (1, 2), {"a"}, b"bytes", True, object()]


@pytest.mark.parametrize("tool", ["memory_remember", "memory_search", "memory_get", "memory_reflect", "memory_related",
                                  "memory_update", "memory_feedback", "memory_forget", "memory_list", "memory_review"])
def test_tool_arguments_that_are_not_an_object_give_a_tool_error(provider, tool):
    for odd in ODD:
        out = json.loads(provider.handle_tool_call(tool, odd))
        assert "error" in out and "object" in out["error"], (tool, odd, out)
    for odd_name in (None, 5, ["memory_get"], {"a": 1}):
        assert "error" in json.loads(provider.handle_tool_call(odd_name, {}))
    assert "error" not in json.loads(provider.handle_tool_call("memory_list", None))        # no arguments at all is fine


def test_tool_argument_values_of_the_wrong_type_never_raise(provider):
    provider.handle_tool_call("memory_remember", {"content": "Zorblax backup runs at 03:00 on node seven.", "kind": "tech"})
    for tool in ("memory_remember", "memory_search", "memory_get", "memory_reflect", "memory_related", "memory_update",
                 "memory_feedback", "memory_forget", "memory_list", "memory_review"):
        for key in ("content", "query", "queries", "id", "ids", "limit", "kind", "keywords", "entities", "entity", "tags", "since",
                    "until", "as_of", "action", "helpful", "pinned", "scope", "detail", "mode", "valid_from", "valid_to"):
            for odd in ODD + [None, {"x": 1}]:
                out = provider.handle_tool_call(tool, {key: odd, "content": "x y z note" if key != "content" else odd, "id": 1}
                                                if key != "id" else {key: odd, "content": "x y z note"})
                assert isinstance(out, str) and isinstance(json.loads(out), dict), (tool, key, odd)


def test_hooks_given_the_wrong_types_never_raise_and_still_work_with_good_ones(provider):
    for odd in [o for o in ODD if not isinstance(o, str)] + [None]:          # (a str is a legitimate argument)
        provider.sync_turn(odd, odd)
        provider.sync_turn("Zorblax backup runs at 03:00 on node seven.", "Noted.", session_id=odd, turn_author=odd)
        assert provider.prefetch(odd) == ""
        provider.on_session_end(odd)
        provider.on_session_end([odd, {"role": odd, "content": odd}, {"role": "user", "content": odd}])
        assert isinstance(provider.on_pre_compress(odd), str)
        assert isinstance(provider.on_pre_compress([odd, {"role": "user", "content": odd}]), str)
        provider.on_memory_write(odd, odd, odd, odd)
        provider.on_memory_write("add", "user", odd)
    # nothing odd was stored as a note, and ordinary calls still work afterwards
    assert provider._store.conn.execute("SELECT COUNT(*) FROM notes WHERE content LIKE '%object%' OR content LIKE '%bytes%'"
                                        " OR content LIKE '%[1]%' OR content LIKE '%2.5%'").fetchone()[0] == 0
    provider.sync_turn("Plinth rota changes on Monday, noted for the team.", "Understood, the rota changes on Monday.")
    assert "Plinth" in provider.prefetch("when does the Plinth rota change")
    provider.on_memory_write("add", "memory", "Quillon reports are due on Fridays.")
    assert provider._store.find_by_content("Quillon reports are due on Fridays.") is not None


def test_an_untrusted_writer_never_changes_an_existing_notes_expiry_by_saving_its_text_again(pkg, tmp_path):
    """MCP without --trust-writes runs the plugin with remember_review set: its re-save must behave as before round 1."""
    import json
    text = "Atlas uses PostgreSQL for orders"
    for mode, expect_cleared in (("auto", False), ("all", False), (None, True)):
        home = tmp_path / f"h-{mode}"
        home.mkdir()
        p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db", **({"remember_review": mode} if mode else {})})
        p.initialize("20261018_120000_abcd", hermes_home=str(home), platform="telegram")
        try:
            nid, _ = p._store.add(text, kind="chat")
            before = _expires(p, nid)
            assert before is not None
            for args in ({}, {"kind": "auto"}, {"kind": "decision"}):
                out = json.loads(p.handle_tool_call("memory_remember", {"content": text, **args}))
                assert out["id"] == nid and out["created"] is False
                if not expect_cleared:
                    assert _expires(p, nid) == before, (mode, args)
            assert (_expires(p, nid) is None) == expect_cleared, mode
            new = json.loads(p.handle_tool_call("memory_remember", {"content": "Zorblax backup runs at 03:00", "kind": "auto"}))
            assert _expires(p, new["id"]) is None               # a NEW note from an untrusted writer is still saved on purpose
        finally:
            p.shutdown()


def test_valid_tool_calls_are_not_errors(provider):
    """The wrong-type test above only checks that a JSON dict comes back; this one catches a handler that always fails."""
    def ok(tool, **args):
        out = json.loads(provider.handle_tool_call(tool, args))
        assert "error" not in out, (tool, args, out)
        return out
    nid = ok("memory_remember", content="Zorblax backup runs at 03:00 on node seven.", kind="tech", keywords="backup", tags="ops",
             entities=["Zorblax"])["id"]
    ok("memory_search", query="Zorblax backup")
    ok("memory_get", id=nid)
    ok("memory_list")
    ok("memory_related", entity="Zorblax")
    ok("memory_reflect", query="Zorblax backup")
    ok("memory_feedback", id=nid, helpful=True)
    ok("memory_update", id=nid, content="Zorblax backup runs at 04:00 on node seven.")
    ok("memory_review", action="list")
    ok("memory_forget", id=nid)


def test_a_broken_handler_gives_a_neutral_error_and_a_warning_with_the_traceback(provider, monkeypatch, caplog):
    import logging
    secret_text = "Zorblax backup runs at 03:00 on node seven."

    def boom(a):
        raise RuntimeError("database disk image is malformed: " + secret_text)
    monkeypatch.setattr(provider, "_search", boom)
    with caplog.at_level(logging.WARNING):
        out = json.loads(provider.handle_tool_call("memory_search", {"query": secret_text}))
    assert out["error"].startswith("memory_search failed (RuntimeError)") and "check the argument types" not in out["error"]
    assert secret_text not in out["error"]
    recs = [r for r in caplog.records if r.levelno == logging.WARNING and "memory_search failed" in r.getMessage()]
    assert recs and recs[0].exc_info and recs[0].exc_info[0] is RuntimeError
    # an argument problem keeps the hint
    monkeypatch.setattr(provider, "_search", lambda a: (_ for _ in ()).throw(OverflowError("int too large")))
    out = json.loads(provider.handle_tool_call("memory_search", {"query": "x"}))
    assert "check the argument types" in out["error"]


def test_prefetch_failures_are_warned_once_per_error_class(provider, monkeypatch, caplog):
    import logging

    def fail(cls):
        def f(q):
            raise cls("nope")
        return f
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(provider, "_prefetch", fail(RuntimeError))
        assert provider.prefetch("anything about backups") == "" and provider.prefetch("again") == ""
        monkeypatch.setattr(provider, "_prefetch", fail(KeyError))
        assert provider.prefetch("third") == ""
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "prefetch failed" in r.getMessage()]
    assert len(warns) == 2 and all(r.exc_info for r in warns)                     # RuntimeError once, KeyError once
    assert [r for r in caplog.records if r.levelno == logging.DEBUG and "prefetch failed" in r.getMessage()]   # the repeat is quiet
