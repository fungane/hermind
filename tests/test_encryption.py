"""Optional SQLCipher encryption: dbdriver.py, the DecisionStore hook, tools/encrypt_db.py, tools/backup.py, hermind-mcp."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sqlcipher3 = pytest.importorskip("sqlcipher3")

ROOT = Path(__file__).resolve().parent.parent
MARK = "ZEBRA-MARKER-8841"


@pytest.fixture
def dbd(pkg):
    return sys.modules["hermind.dbdriver"]


@pytest.fixture
def Store(pkg):
    return sys.modules["hermind.store"].DecisionStore


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HERMIND_DB_KEY", raising=False)
    monkeypatch.delenv("HERMIND_DB_KEY_FILE", raising=False)


def _fill(Store, path, key=None):
    s = Store(str(path), key=key)
    s.add(f"The team deploys on Fridays, code {MARK}.", "decision", "deploy friday", pinned=True)
    s.add("Backups are copied weekly to the encrypted drive.", "decision", "backup weekly")
    s.close()


def _raw_bytes(path):
    folder = Path(path).parent
    return b"".join(p.read_bytes() for p in folder.iterdir() if p.name.startswith(Path(path).name))


def test_off_by_default_is_plain_sqlite_and_never_imports_the_driver(tmp_path, Store, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)               # any import of it would now raise
    s = Store(str(tmp_path / "plain.db"))
    assert s.encrypted is False and isinstance(s.conn, sqlite3.Connection)
    s.close()


def test_encrypted_store_works_and_leaves_no_plaintext(tmp_path, Store):
    p = tmp_path / "enc.db"
    _fill(Store, p, key="correct horse")
    raw = _raw_bytes(p)
    assert MARK.encode() not in raw and b"deploys on Fridays" not in raw and raw[:15] != b"SQLite format 3"
    s = Store(str(p), key="correct horse")
    assert s.encrypted is True
    assert any(MARK in r["content"] for r in s.search("deploys Fridays", limit=3))
    s.close()


def test_wrong_key_and_missing_key_have_clear_errors(tmp_path, Store, dbd):
    p = tmp_path / "enc.db"
    _fill(Store, p, key="right")
    with pytest.raises(dbd.EncryptionError, match="wrong key"):
        Store(str(p), key="wrong")
    with pytest.raises(dbd.EncryptionError, match="HERMIND_DB_KEY_FILE"):
        Store(str(p))


def test_key_on_a_plaintext_file_points_to_the_tool(tmp_path, Store, dbd):
    p = tmp_path / "plain.db"
    _fill(Store, p)
    with pytest.raises(dbd.EncryptionError, match="encrypt_db.py"):
        Store(str(p), key="k")


def test_missing_driver_says_how_to_install(tmp_path, Store, dbd, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises(dbd.EncryptionError, match=r"hermind\[encrypted\]"):
        Store(str(tmp_path / "x.db"), key="k")


def test_key_sources(tmp_path, dbd):
    f = tmp_path / "k"
    f.write_text("  filekey \n")
    assert dbd.key_from_env({"HERMIND_DB_KEY_FILE": str(f), "HERMIND_DB_KEY": "envkey"}) == "filekey"   # file wins
    assert dbd.key_from_env({"HERMIND_DB_KEY": "envkey"}) == "envkey"
    assert dbd.key_from_env({}) is None
    with pytest.raises(dbd.EncryptionError, match="cannot read"):
        dbd.key_from_env({"HERMIND_DB_KEY_FILE": str(tmp_path / "nope")})
    f.write_text("\n")
    with pytest.raises(dbd.EncryptionError, match="empty"):
        dbd.key_from_env({"HERMIND_DB_KEY_FILE": str(f)})


def test_key_with_quote_characters(tmp_path, Store):
    p = tmp_path / "q.db"
    _fill(Store, p, key="it's a \"key\"; DROP TABLE notes;--")
    s = Store(str(p), key="it's a \"key\"; DROP TABLE notes;--")
    assert s.conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    s.close()


def _tool(*args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, str(ROOT / "tools" / "encrypt_db.py"), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_encrypt_tool_roundtrip(tmp_path, Store):
    db = tmp_path / "m.db"
    _fill(Store, db)
    keyfile = tmp_path / "db.key"
    r = _tool("--generate-key-file", str(keyfile))
    assert r.returncode == 0 and oct(keyfile.stat().st_mode & 0o777) == "0o600"
    assert _tool("--generate-key-file", str(keyfile)).returncode != 0          # never overwrites a key
    env = {"HERMIND_DB_KEY_FILE": str(keyfile)}
    before = db.read_bytes()
    r = _tool(str(db), env_extra=env)                                           # dry copy first
    assert r.returncode == 0 and "verified" in r.stdout, r.stdout + r.stderr
    assert db.read_bytes() == before and (tmp_path / "m.db.encrypted").exists()
    (tmp_path / "m.db.encrypted").unlink()
    r = _tool(str(db), "--replace", env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "m.db.plaintext-backup").exists() and (tmp_path / "m.db.plaintext-backup").read_bytes()[:15] == b"SQLite format 3"
    assert MARK.encode() not in db.read_bytes()
    s = Store(str(db), key=keyfile.read_text().strip())
    assert any(MARK in x["content"] for x in s.search("deploys Fridays", limit=3))
    s.close()
    assert "already encrypted" in _tool(str(db), env_extra=env).stderr
    r = _tool(str(db), "--decrypt", "--replace", env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert db.read_bytes()[:15] == b"SQLite format 3"
    s = Store(str(db))
    assert s.conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    s.close()


def test_encrypt_tool_needs_a_key_and_a_database(tmp_path, Store):
    db = tmp_path / "m.db"
    _fill(Store, db)
    assert "HERMIND_DB_KEY_FILE" in _tool(str(db)).stderr
    assert _tool(str(tmp_path / "missing.db"), env_extra={"HERMIND_DB_KEY": "k"}).returncode != 0


def test_backup_of_an_encrypted_database_stays_encrypted(tmp_path, Store):
    db = tmp_path / "m.db"
    _fill(Store, db, key="k1")
    dest = tmp_path / "bk"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "backup.py"), "--dest", str(dest), str(db)],
                       capture_output=True, text=True, env={**os.environ, "HERMIND_DB_KEY": "k1"}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    copy = next(dest.glob("*.db"))
    assert MARK.encode() not in copy.read_bytes()
    s = Store(str(copy), key="k1")
    assert s.conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    s.close()


def test_mcp_server_reads_an_encrypted_database(tmp_path, Store, mcp_spawn):
    db = tmp_path / "m.db"
    _fill(Store, db, key="k1")
    c = mcp_spawn("--db", str(db), env={"HERMIND_DB_KEY": "k1"})
    res = c.request("tools/call", {"name": "memory_search", "arguments": {"query": "deploys Fridays"}})["result"]
    assert not res.get("isError") and MARK in res["content"][0]["text"]


def test_mcp_server_without_the_key_fails_with_a_clear_message(tmp_path, Store):
    db = tmp_path / "m.db"
    _fill(Store, db, key="k1")
    r = subprocess.run([sys.executable, str(ROOT / "hermind_mcp.py"), "--db", str(db)], input="",
                       capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in os.environ.items() if not k.startswith("HERMIND_DB_KEY")})
    assert r.returncode != 0 and "HERMIND_DB_KEY_FILE" in r.stderr


def test_vector_worker_failure_leaves_a_log(tmp_path, pkg):
    """A worker that cannot start (e.g. no sqlcipher3 in its venv) must not fail silently."""
    server = tmp_path / "bad_server.py"
    server.write_text("import sys\nsys.stderr.write('ModuleNotFoundError: No module named sqlcipher3\\n')\nsys.exit(1)\n")
    model = tmp_path / "model.onnx"
    model.write_text("x")
    db = tmp_path / "m.db"
    import importlib
    vec = importlib.import_module("hermind.vec").VectorIndex({"python": sys.executable, "server": str(server), "model": str(model)}, str(db))
    assert vec._call({"op": "ping"}, timeout=5) is None
    log = tmp_path / "m.db.vec.log"
    assert "sqlcipher3" in log.read_text() and oct(log.stat().st_mode & 0o777) == "0o600"
    vec.close()
