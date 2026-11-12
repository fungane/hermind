import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cli(home, *args):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("HERMIND_DB", None)
    return subprocess.run([sys.executable, str(ROOT / "hermind_cli.py"), *args], capture_output=True, text=True, env=env)


def test_export_to_an_unwritable_place_is_one_line_not_a_traceback(tmp_path):
    _cli(tmp_path, "remember", "Atlas uses PostgreSQL")
    r = _cli(tmp_path, "export", "--out", str(tmp_path / "no" / "such" / "dir" / "e.json"))
    assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout and "cannot use" in r.stdout + r.stderr


def test_database_path_that_is_a_directory_is_one_line_not_a_traceback(tmp_path):
    (tmp_path / "adir").mkdir()
    for cmd in (("search", "orders"), ("remember", "x")):
        r = _cli(tmp_path, *cmd, "--db", str(tmp_path / "adir"))
        assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout, (cmd, r.stdout, r.stderr)


def test_search_limit_must_be_positive(tmp_path):
    _cli(tmp_path, "remember", "Atlas uses PostgreSQL")
    for bad in ("0", "-3"):
        r = _cli(tmp_path, "search", "atlas", "--limit", bad)
        assert r.returncode == 2 and "must be 1 or more" in r.stderr
    assert _cli(tmp_path, "search", "atlas", "--limit", "1").returncode == 0


def test_mcp_lone_surrogate_gets_a_clear_message(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    _cli(tmp_path, "remember", "orders note", "--db", str(tmp_path / "m.db"))
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    call = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"\\ud800 orders"}}}'
    r = subprocess.run([sys.executable, str(ROOT / "hermind_mcp.py"), "--db", str(tmp_path / "m.db")], input=init + "\n" + call + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    last = json.loads(r.stdout.strip().splitlines()[-1])
    assert last["result"]["isError"] is True and "cannot be stored" in last["result"]["content"][0]["text"]
    assert "codec" not in last["result"]["content"][0]["text"]


def _provider(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("hermind_plugin_guards", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermind_plugin_guards"] = mod
    spec.loader.exec_module(mod)
    p = mod.HermindProvider(config={"db_path": str(tmp_path / "p.db")})
    p.initialize("guards", hermes_home=str(tmp_path), platform="cli")
    return p


def test_plugin_tools_never_raise_on_wrongly_typed_arguments(tmp_path):
    p = _provider(tmp_path)
    p.handle_tool_call("memory_remember", {"content": "Atlas uses PostgreSQL", "kind": "decision"})
    for args in ({"kind": {}}, {"kind": []}, {"limit": {}}, {"limit": "x"}, {"kind": 5, "limit": 10 ** 12}):
        out = p.handle_tool_call("memory_list", args)
        assert isinstance(out, str) and json.loads(out) is not None


def test_session_end_ignores_junk_message_entries(tmp_path):
    p = _provider(tmp_path)
    p.on_session_end([{"role": "user", "content": None}, "junk", 5, {"role": "assistant"}])   # must not raise


def test_a_file_that_is_not_a_database_gives_one_line_in_every_database_command(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(os.urandom(5000))
    for cmd in (("search", "x"), ("remember", "x"), ("export",), ("review",)):
        r = _cli(tmp_path, *cmd, "--db", str(bad))
        assert r.returncode == 2 and "Traceback" not in r.stdout + r.stderr, (cmd, r.stdout, r.stderr)
        assert "not a SQLite database" in r.stdout + r.stderr, cmd


def test_invisible_characters_do_not_split_words():
    sys.path.insert(0, str(ROOT))
    from textnorm import normalize
    assert normalize("Postgre\u200bSQL\ufeff") == "postgresql"
    assert normalize("data\u00adbase") == "database"
