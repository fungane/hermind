import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"


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


# -- the scripts in tools/: one line and a nonzero exit, never a traceback ---------------------------------------------------


def _tool(home, name, *args):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    for k in [k for k in env if k.startswith("HERMIND_")]:
        del env[k]
    return subprocess.run([sys.executable, str(ROOT / "tools" / name), *args], capture_output=True, text=True, env=env, timeout=60)


def _make_db(path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    s = DecisionStore(str(path))
    s.add("Zorblax backup runs at 03:00 on node seven.", kind="tech")
    s.close()


def test_backup_keep_must_be_one_or_more_and_never_deletes_on_a_bad_value(tmp_path):
    db = tmp_path / "m.db"
    _make_db(db)
    dest = tmp_path / "bk"
    r = _tool(tmp_path, "backup.py", "--dest", str(dest), str(db))
    assert r.returncode == 0 and len(list(dest.glob("*.db"))) == 1
    for bad in ("-1", "0", "x"):
        r = _tool(tmp_path, "backup.py", "--dest", str(dest), "--keep", bad, str(db))
        assert r.returncode == 2 and "--keep" in r.stderr and "Traceback" not in r.stderr, (bad, r.stderr)
        assert len(list(dest.glob("*.db"))) == 1                                  # nothing was deleted
    ok = _tool(tmp_path, "backup.py", "--dest", str(dest), "--keep", "1", str(db))
    assert ok.returncode == 0 and "kept 1" in ok.stdout


def test_backup_of_a_missing_database_says_so(tmp_path):
    r = _tool(tmp_path, "backup.py", "--dest", str(tmp_path / "bk"), str(tmp_path / "none.db"))
    assert r.returncode == 1 and "database not found" in r.stderr and str(tmp_path / "none.db") in r.stderr
    assert "Traceback" not in r.stderr
    db = tmp_path / "m.db"
    _make_db(db)
    r = _tool(tmp_path, "backup.py", "--dest", str(tmp_path / "bk"), str(db), str(tmp_path / "none.db"))
    assert r.returncode == 1 and "database not found" in r.stderr             # one missing among several is still a failure


@pytest.mark.xfail(sys.platform == "win32", strict=True, reason=(
    "Windows: when a parent folder of the key file cannot be made, Path.mkdir raises FileExistsError there, which "
    "encrypt_db.py reports as '<key file> already exists' instead of 'cannot write'"))
def test_generate_key_file_does_not_overwrite_and_says_so(tmp_path):
    key = tmp_path / "db.key"
    key.write_text("keep me\n", encoding="utf-8")
    r = _tool(tmp_path, "encrypt_db.py", "--generate-key-file", str(key))
    assert r.returncode != 0 and "already exists" in r.stderr and "Traceback" not in r.stderr
    assert key.read_text(encoding="utf-8") == "keep me\n"
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    r = _tool(tmp_path, "encrypt_db.py", "--generate-key-file", str(blocked / "sub" / "db.key"))
    assert r.returncode != 0 and "cannot write" in r.stderr and "Traceback" not in r.stderr


def test_export_hindsight_without_a_server_is_one_line(tmp_path):
    r = _tool(tmp_path, "export_hindsight.py", "bank", str(tmp_path / "out.json"), "--url", "http://127.0.0.1:1")
    assert r.returncode != 0 and "cannot reach http://127.0.0.1:1" in r.stderr and "Traceback" not in r.stderr
    assert not (tmp_path / "out.json").exists()


@pytest.mark.xfail(sys.platform == "win32", strict=True, reason=(
    "Windows: a write to a closed pipe raises OSError EINVAL, not BrokenPipeError, so the quiet exit does not run and "
    "Python prints 'Exception ignored ... OSError: [Errno 22]' to stderr at exit"))
def test_closed_pipe_ends_the_command_quietly(tmp_path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    for i in range(30):                                         # about 1.2 MB of JSON: far more than a pipe buffer holds
        s.add(f"Zorblax note {i}: " + " ".join(f"word{i}x{j}" for j in range(4000)), kind="tech")
    s.close()
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.pop("HERMIND_DB", None)
    for cmd in (["export", "--db", str(db)], ["search", "zorblax", "--db", str(db), "--limit", "20"]):
        p = subprocess.Popen([sys.executable, str(ROOT / "hermind_cli.py"), *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p.stdout.readline()
        p.stdout.close()                                         # like `| head -1`
        err = p.stderr.read()
        code = p.wait(timeout=60)
        p.stderr.close()
        assert err == b"", err.decode(errors="replace")[:300]
        assert code in (0, 1), code


import pytest  # noqa: E402


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell to close stdout")
def test_closed_stdout_still_saves_the_note_and_exits_zero(tmp_path):
    """Round 2: `hermind remember ... >&-` saved the note, then died with AttributeError on sys.stdout.flush()."""
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.pop("HERMIND_DB", None)
    db = tmp_path / "c.db"
    for cmd in (f'"{sys.executable}" "{ROOT}/hermind_cli.py" remember "Zorblax backup runs at 03:00" --db "{db}" >&-',
                f'"{sys.executable}" "{ROOT}/hermind_cli.py" remember "Quillon reports are due on Fridays" --db "{db}" --json >&-'):
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "Traceback" not in r.stderr and r.stderr == "", (cmd, r.returncode, r.stderr)
    r = _cli(tmp_path, "search", "zorblax", "--db", str(db))
    assert "Zorblax backup runs at 03:00" in r.stdout
