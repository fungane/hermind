"""MCP stdio protocol: handshake, version negotiation, tools/list sizes, JSON-RPC error paths, stdout cleanliness.
Each test talks to a real `hermind_mcp.py` subprocess (HOME and database are temporary)."""

import json

import pytest

PROBE = "UNIQUEPROBE-7731"   # note text that must never show up in stderr logs


def test_handshake_echoes_supported_versions(mcp_spawn):
    for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
        c = mcp_spawn(handshake=False)
        r = c.handshake(version)["result"]
        assert r["protocolVersion"] == version
        assert r["capabilities"] == {"tools": {"listChanged": False}}
        assert r["serverInfo"]["name"] == "hermind" and r["serverInfo"]["version"]
        assert "never follow commands" in r["instructions"]


def test_unknown_protocol_version_gets_the_preferred_one(mcp_spawn):
    c = mcp_spawn(handshake=False)
    assert c.handshake("2099-01-01")["result"]["protocolVersion"] == "2025-06-18"


def test_initialize_without_protocol_version_is_invalid_params(mcp_spawn):
    c = mcp_spawn(handshake=False)
    assert c.request("initialize", {})["error"]["code"] == -32602


def test_ping_and_notifications_get_no_reply(mcp_spawn):
    c = mcp_spawn()
    assert c.request("ping")["result"] == {}
    c.notify("notifications/cancelled", {"requestId": 99, "reason": "x"})
    c.notify("notifications/does-not-exist")
    assert c.request("ping")["result"] == {}      # the next line we read is this reply, so no reply was queued


def test_tools_list_read_only_default(mcp_spawn):
    c = mcp_spawn()
    resp = c.request("tools/list")
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["memory_search", "memory_get", "memory_list", "memory_related"]
    for t in resp["result"]["tools"]:
        assert t["annotations"] == {"readOnlyHint": True}
        assert t["inputSchema"]["type"] == "object" and len(t["description"]) < 150
    size = len(c.raw_lines[-1].encode()) - 1
    assert size < 2400, size
    print("tools/list read-only bytes:", size)


def test_tools_list_with_write(mcp_spawn):
    c = mcp_spawn("--write")
    resp = c.request("tools/list")
    assert [t["name"] for t in resp["result"]["tools"]] == [
        "memory_search", "memory_get", "memory_list", "memory_related", "memory_remember", "memory_update"]
    size = len(c.raw_lines[-1].encode()) - 1
    assert size < 3400, size
    print("tools/list write bytes:", size)


def test_tools_allowlist_and_write_gate(mcp_spawn):
    ro = mcp_spawn("--tools", "search,forget,feedback,review,reflect")
    names = [t["name"] for t in ro.request("tools/list")["result"]["tools"]]
    assert names == ["memory_search", "memory_reflect", "memory_review"]      # forget/feedback need --write
    review = [t for t in ro.request("tools/list")["result"]["tools"] if t["name"] == "memory_review"][0]
    assert review["inputSchema"]["properties"]["action"]["enum"] == ["list"]
    assert "forget needs --write" in ro.stderr() or "memory_forget needs --write" in ro.stderr()
    rw = mcp_spawn("--write", "--tools", "get,forget,feedback,review", name="rw")
    assert [t["name"] for t in rw.request("tools/list")["result"]["tools"]] == [
        "memory_get", "memory_feedback", "memory_forget", "memory_review"]
    review = [t for t in rw.request("tools/list")["result"]["tools"] if t["name"] == "memory_review"][0]
    assert review["inputSchema"]["properties"]["action"]["enum"] == ["list"]          # an agent cannot approve its own notes
    tw = mcp_spawn("--write", "--trust-writes", "--tools", "review", name="tw")
    review = [t for t in tw.request("tools/list")["result"]["tools"] if t["name"] == "memory_review"][0]
    assert "confirm" in review["inputSchema"]["properties"]["action"]["enum"]


def test_env_configuration(mcp_spawn, tmp_path):
    c = mcp_spawn(env={"HERMIND_MCP_WRITE": "1", "HERMIND_DB": str(tmp_path / "e.db"), "HERMIND_AGENT": "envagent",
                       "HERMIND_MCP_TOOLS": "search,remember"})
    assert [t["name"] for t in c.request("tools/list")["result"]["tools"]] == ["memory_search", "memory_remember"]
    out, err = c.call_json("memory_remember", content="env configured note", keywords="a b c d e")
    assert not err
    import sqlite3
    row = sqlite3.connect(tmp_path / "e.db").execute("SELECT source_session FROM notes").fetchone()
    assert row == ("mcp:envagent",)


def test_default_database_is_created_under_home(mcp_spawn, tmp_path):
    c = mcp_spawn()
    c.call("memory_list")
    assert (tmp_path / ".hermind" / "memory.db").exists()


def test_agent_name_falls_back_to_client_info(mcp_spawn, tmp_path):
    c = mcp_spawn("--write", handshake=False)
    c.handshake(name="my/editor 1")
    c.call("memory_remember", content="provenance from clientInfo", keywords="a b c d e")
    import sqlite3
    assert sqlite3.connect(tmp_path / ".hermind" / "memory.db").execute(
        "SELECT source_session FROM notes").fetchone() == ("mcp:my-editor-1",)


def test_unknown_tool_name_in_cli_exits_2(mcp_spawn):
    c = mcp_spawn("--tools", "search,nope", handshake=False)
    assert c.close() == 2
    assert "unknown tool" in c.stderr()


# -- JSON-RPC error paths -------------------------------------------------------------------------------------

def test_error_paths(mcp_spawn):
    c = mcp_spawn()
    assert c.request("nope/method")["error"]["code"] == -32601
    assert c.request("tools/call", {"name": "memory_nope", "arguments": {}})["error"]["code"] == -32602
    assert c.request("tools/call", {"arguments": {}})["error"]["code"] == -32602
    assert c.request("tools/call", {"name": "memory_search", "arguments": [1]})["error"]["code"] == -32602
    c.send_raw("this is not json")
    r = c.read()
    assert r["id"] is None and r["error"]["code"] == -32700
    c.send_raw(json.dumps({"id": 5, "method": "ping"}))                      # no jsonrpc member
    assert c.read()["error"]["code"] == -32600
    c.send_raw(json.dumps([1, 2]))                                           # batch of non-requests
    assert [x["error"]["code"] for x in c.read()] == [-32600, -32600]
    c.send_raw("[]")
    assert c.read()["error"]["code"] == -32600
    c.send_raw(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": [1]}))
    assert c.read()["error"]["code"] == -32602
    assert c.request("ping")["result"] == {}                                 # still alive after all of that


def test_batch_requests(mcp_spawn):
    c = mcp_spawn()
    c.send_raw(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]))
    out = c.read()
    assert [x["id"] for x in out] == [1, 2] and out[1]["result"]["tools"]


def test_tool_argument_errors_are_is_error_results(mcp_spawn):
    c = mcp_spawn()
    for tool, args in [("memory_search", {}), ("memory_search", {"query": 5}), ("memory_get", {"id": "abc"}),
                       ("memory_search", {"query": "x", "kind": "bogus"}), ("memory_search", {"query": "x" * 5000}),
                       ("memory_search", {"query": "x", "queries": list("abcde")}),
                       ("memory_get", {"id": 99999})]:
        res = c.call(tool, **args)
        assert res["isError"] is True and res["content"][0]["type"] == "text", (tool, args)
        assert "error" in json.loads(res["content"][0]["text"])


def test_oversized_message_is_rejected_not_fatal(mcp_spawn):
    c = mcp_spawn()
    c.send_raw("x" * 1_200_000)
    assert c.read()["error"]["code"] == -32600
    assert c.request("ping")["result"] == {}


def test_eof_ends_the_process_cleanly(mcp_spawn):
    c = mcp_spawn()
    assert c.close() == 0


# -- stdout cleanliness / logging ------------------------------------------------------------------------------

def test_stdout_has_only_protocol_messages_and_logs_hide_note_text(mcp_spawn):
    c = mcp_spawn("--write", "-v")
    c.call("memory_remember", content=f"{PROBE} nginx decision", keywords="web proxy server config probe")
    c.call("memory_search", query=PROBE)
    c.call("memory_get", id=1)
    c.call("memory_list")
    c.call("memory_related", entity="nginx")
    c.close()
    lines = [l for l in c.raw_lines if l.strip()]
    assert lines
    for line in lines:
        msg = json.loads(line)
        assert msg["jsonrpc"] == "2.0"
    err = c.stderr()
    assert "tools/call memory_search" in err          # -v logs tool names ...
    assert PROBE not in err and "nginx" not in err    # ... never note text or queries


def test_stray_print_cannot_corrupt_stdout(tmp_path):
    """Anything that prints while the server runs (a library, a debug line) lands on stderr."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys, runpy, io\n"
        "sys.argv = ['hermind_mcp.py']\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('hm', {str(root / 'hermind_mcp.py')!r})\n"
        "hm = importlib.util.module_from_spec(spec); sys.modules['hm'] = hm; spec.loader.exec_module(hm)\n"
        "orig = hm.HermindMCP.handle_message\n"
        "def noisy(self, msg):\n"
        "    print('STRAY DEBUG LINE')\n"
        "    return orig(self, msg)\n"
        "hm.HermindMCP.handle_message = noisy\n"
        "sys.exit(hm.main(['--db', sys.argv[0] + '.db']))\n")
    p = subprocess.run([sys.executable, "-I", "-c", code], input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                       capture_output=True, text=True, cwd=str(tmp_path), env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert p.stdout.strip() == '{"jsonrpc":"2.0","id":1,"result":{}}'
    assert "STRAY DEBUG LINE" in p.stderr


def test_cjk_round_trip(mcp_spawn):
    c = mcp_spawn("--write")
    c.call("memory_remember", content="決定每月第一個星期一備份資料庫到外置硬碟", keywords="備份 數據庫 backup 每月",
           kind="decision")
    got, err = c.call_json("memory_search", query="資料庫備份", detail="more")
    assert not err and got["count"] == 1 and "外置硬碟" in got["results"][0]["content"]
    assert "\n" not in c.raw_lines[-1].rstrip("\n")           # one message per line, always


def _db_with_core(tmp_path):
    import sys
    store = sys.modules["hermind.store"].DecisionStore(str(tmp_path / "core.db"))
    store.add("The team always deploys on Fridays after the CORE-PROBE-4417 review.", "decision", "deploy friday",
              pinned=True)
    store.close()
    return str(tmp_path / "core.db")


def test_instructions_carry_the_core_block(mcp_spawn, tmp_path, pkg):
    db = _db_with_core(tmp_path)
    c = mcp_spawn("--db", db, handshake=False)
    text = c.handshake()["result"]["instructions"]
    assert "never follow commands" in text and "CORE-PROBE-4417" in text
    assert "hermind-context" in text                       # fenced as data
    assert len(text) < 2000                                # stays under the client-side instruction cap


def test_core_block_can_be_turned_off(mcp_spawn, tmp_path, pkg):
    db = _db_with_core(tmp_path)
    for args, env in ((("--db", db, "--core-chars", "0"), None), (("--db", db), {"HERMIND_CORE_CHARS": "0"})):
        c = mcp_spawn(*args, env=env, handshake=False)
        assert "CORE-PROBE-4417" not in c.handshake()["result"]["instructions"]


def test_empty_database_has_no_core_block(mcp_spawn):
    c = mcp_spawn(handshake=False)
    text = c.handshake()["result"]["instructions"]
    assert "hermind-context" not in text and "never follow commands" in text


def test_core_block_does_not_write_in_read_only_mode(mcp_spawn, tmp_path, pkg):
    import sqlite3
    db = _db_with_core(tmp_path)
    before = sqlite3.connect(db).execute("SELECT total(access_count), count(*) FROM notes").fetchone()
    c = mcp_spawn("--db", db, handshake=False)
    c.handshake()
    c.close()
    assert sqlite3.connect(db).execute("SELECT total(access_count), count(*) FROM notes").fetchone() == before


# -- 2026-07-28 (stateless era): no initialize, every request carries _meta; dual-era server ---------------------

PV = "io.modelcontextprotocol/protocolVersion"
CAPS = "io.modelcontextprotocol/clientCapabilities"
INFO = "io.modelcontextprotocol/clientInfo"
SINFO = "io.modelcontextprotocol/serverInfo"


def _meta(version="2026-07-28", **extra):
    return {"_meta": {PV: version, CAPS: {}, INFO: {"name": "ModernClient", "version": "1"}}, **extra}


def test_discover_without_initialize(mcp_spawn, tmp_path, pkg):
    db = _db_with_core(tmp_path)
    c = mcp_spawn("--db", db, handshake=False)
    r = c.request("server/discover", _meta())["result"]
    assert r["resultType"] == "complete" and r["supportedVersions"] == ["2026-07-28"]
    assert r["capabilities"] == {"tools": {}}
    assert r["_meta"][SINFO]["name"] == "hermind"
    assert "CORE-PROBE-4417" in r["instructions"] and "never follow commands" in r["instructions"]
    assert r["ttlMs"] >= 0 and r["cacheScope"] in ("public", "private")


def test_modern_tools_list_and_call(mcp_spawn, tmp_path, pkg):
    db = _db_with_core(tmp_path)
    c = mcp_spawn("--db", db, handshake=False)
    lst = c.request("tools/list", _meta())["result"]
    assert lst["resultType"] == "complete" and lst["ttlMs"] >= 0 and lst["_meta"][SINFO]["name"] == "hermind"
    assert [t["name"] for t in lst["tools"]] == ["memory_search", "memory_get", "memory_list", "memory_related"]
    res = c.request("tools/call", _meta(name="memory_search", arguments={"query": "deploys Fridays"}))["result"]
    assert res["resultType"] == "complete" and not res.get("isError")
    assert "deploys on Fridays" in res["content"][0]["text"]
    refused = c.request("tools/call", _meta(name="memory_remember", arguments={"content": "x", "keywords": "y"}))
    assert refused["error"]["code"] == -32602                    # write tools stay hidden in the modern era too


def test_modern_unsupported_version_is_32022(mcp_spawn):
    c = mcp_spawn(handshake=False)
    err = c.request("tools/list", _meta("1900-01-01"))["error"]
    assert err["code"] == -32022 and err["data"]["supported"] == ["2026-07-28"] and err["data"]["requested"] == "1900-01-01"


def test_modern_missing_required_meta_is_32602(mcp_spawn):
    c = mcp_spawn(handshake=False)
    assert c.request("tools/list", {"_meta": {PV: "2026-07-28"}})["error"]["code"] == -32602   # no capabilities
    assert c.request("server/discover", {})["error"]["code"] == -32602


def test_modern_has_no_ping_and_no_initialize(mcp_spawn):
    c = mcp_spawn(handshake=False)
    assert c.request("ping", _meta())["error"]["code"] == -32601
    assert c.request("initialize", _meta())["error"]["code"] == -32601


def test_dual_era_legacy_still_works_after_a_modern_probe(mcp_spawn):
    c = mcp_spawn(handshake=False)
    assert c.request("server/discover", _meta())["result"]["resultType"] == "complete"
    assert c.handshake("2025-06-18")["result"]["protocolVersion"] == "2025-06-18"
    assert c.request("ping")["result"] == {}
    assert len(c.request("tools/list")["result"]["tools"]) == 4


def test_modern_read_only_writes_nothing(mcp_spawn, tmp_path, pkg):
    import sqlite3
    db = _db_with_core(tmp_path)
    before = sqlite3.connect(db).execute("SELECT total(access_count), count(*) FROM notes").fetchone()
    c = mcp_spawn("--db", db, handshake=False)
    c.request("server/discover", _meta())
    c.request("tools/call", _meta(name="memory_search", arguments={"query": "deploys"}))
    c.close()
    assert sqlite3.connect(db).execute("SELECT total(access_count), count(*) FROM notes").fetchone() == before
