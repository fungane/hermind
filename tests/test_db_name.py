import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))
from store_common import default_db_file, with_legacy_fallback  # noqa: E402


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(p).close()
    return p


def test_a_new_install_gets_hermind_db(tmp_path):
    assert default_db_file(tmp_path) == tmp_path / "hermind.db"


def test_an_existing_install_keeps_its_old_file_and_nothing_is_moved(tmp_path):
    old = _touch(tmp_path / "decision_memory.db")
    assert default_db_file(tmp_path) == old
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decision_memory.db"]      # no implicit rename or copy


def test_once_hermind_db_exists_it_wins(tmp_path):
    _touch(tmp_path / "decision_memory.db")
    new = _touch(tmp_path / "hermind.db")
    assert default_db_file(tmp_path) == new


def test_explicit_paths_are_respected_but_a_missing_hermind_db_falls_back_to_a_sibling(tmp_path):
    old = _touch(tmp_path / "decision_memory.db")
    assert with_legacy_fallback(tmp_path / "hermind.db") == old
    other = tmp_path / "notes.db"
    assert with_legacy_fallback(other) == other                                       # only the standard name is substituted
    assert with_legacy_fallback(tmp_path / "sub" / "hermind.db") == tmp_path / "sub" / "hermind.db"


def test_mcp_server_uses_the_old_file_when_that_is_all_there_is(tmp_path):
    import hermind_mcp
    old = _touch(tmp_path / "decision_memory.db")
    assert hermind_mcp._with_legacy_fallback(str(tmp_path / "hermind.db")) == str(old)
    assert hermind_mcp._with_legacy_fallback(str(tmp_path / "x.db")) == str(tmp_path / "x.db")
    new = _touch(tmp_path / "hermind.db")
    assert hermind_mcp._with_legacy_fallback(str(new)) == str(new)


def test_the_plugin_resolves_its_default_path_the_same_way(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("hermind_plugin_dbname", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermind_plugin_dbname"] = mod
    spec.loader.exec_module(mod)
    prov = mod.HermindProvider(config={})
    assert prov._db_path(str(tmp_path)) == str(tmp_path / "hermind.db")
    _touch(tmp_path / "decision_memory.db")
    assert prov._db_path(str(tmp_path)) == str(tmp_path / "decision_memory.db")
    explicit = mod.HermindProvider(config={"db_path": str(tmp_path / "mine.db")})
    assert explicit._db_path(str(tmp_path)) == str(tmp_path / "mine.db")


def _cli(*args, home):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(home / ".hermes")}
    return subprocess.run([sys.executable, str(ROOT / "hermind_cli.py"), *args], capture_output=True, text=True, env=env)


def _notes_db(path: Path, text="Atlas uses PostgreSQL"):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT)")
    c.execute("INSERT INTO notes(content) VALUES (?)", (text,))
    c.commit()
    c.close()


def test_rename_db_is_a_dry_run_until_apply(tmp_path):
    h = tmp_path / ".hermes"
    _notes_db(h / "decision_memory.db")
    r = _cli("rename-db", "--json", home=tmp_path)
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["apply"] is False
    assert (h / "decision_memory.db").exists() and not (h / "hermind.db").exists()


def test_rename_db_apply_moves_the_file_keeps_the_data_and_leaves_a_backup(tmp_path):
    h = tmp_path / ".hermes"
    _notes_db(h / "decision_memory.db")
    (h / "decision_memory.db.vec.log").write_text("")
    r = _cli("rename-db", "--apply", "--json", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (h / "decision_memory.db").exists() and (h / "hermind.db").exists() and (h / "hermind.db.vec.log").exists()
    assert sqlite3.connect(h / "hermind.db").execute("SELECT content FROM notes").fetchone()[0] == "Atlas uses PostgreSQL"
    baks = list(h.glob("decision_memory.db.bak-rename-*"))
    assert len(baks) == 1 and sqlite3.connect(baks[0]).execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    if os.name != "nt":
        assert oct(baks[0].stat().st_mode & 0o777) == "0o600"


def test_rename_db_refuses_when_hermind_db_exists_or_the_file_is_in_use(tmp_path):
    h = tmp_path / ".hermes"
    _notes_db(h / "decision_memory.db")
    _notes_db(h / "hermind.db", "other")
    r = _cli("rename-db", "--apply", "--json", home=tmp_path)
    assert r.returncode == 2 and "already exists" in json.loads(r.stdout)["error"]
    (h / "hermind.db").unlink()
    holder = sqlite3.connect(h / "decision_memory.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        r = _cli("rename-db", "--apply", "--json", home=tmp_path)
        assert r.returncode == 2 and "open" in json.loads(r.stdout)["error"]
        assert (h / "decision_memory.db").exists() and not (h / "hermind.db").exists()
    finally:
        holder.close()


def _hermind_db(path: Path, texts=("Atlas uses PostgreSQL",)):
    """A database the way Hermind leaves it: WAL mode."""
    from store import DecisionStore
    s = DecisionStore(str(path))
    for t in texts:
        s.add(t, "state")
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.conn.close()


@pytest.mark.parametrize("other", ["connected and idle", "in the middle of a read"])
def test_rename_db_refuses_while_another_program_is_connected_in_wal_mode(tmp_path, other):
    """In WAL mode a program that is connected but idle (an agent between two turns), or reading, holds no write lock;
    it must still stop the rename, and no backup copy is left behind."""
    h = tmp_path / ".hermes"
    _hermind_db(h / "decision_memory.db")
    holder = sqlite3.connect(h / "decision_memory.db", isolation_level=None)
    if other == "in the middle of a read":
        holder.execute("BEGIN")
    holder.execute("SELECT count(*) FROM notes").fetchone()
    try:
        r = _cli("rename-db", "--apply", "--json", home=tmp_path)
        assert r.returncode == 2 and "open" in json.loads(r.stdout)["error"], r.stdout + r.stderr
        assert (h / "decision_memory.db").exists() and not (h / "hermind.db").exists()
        assert not list(h.glob("decision_memory.db.bak-rename-*"))
    finally:
        holder.close()


def test_rename_db_of_a_wal_database_keeps_every_note_and_wal_comes_back_on_the_next_open(tmp_path):
    from store import DecisionStore
    h = tmp_path / ".hermes"
    texts = ("Atlas uses PostgreSQL", "Orion ships on Fridays", "Maria prefers vegetarian lunches")
    _hermind_db(h / "decision_memory.db", texts)
    r = _cli("rename-db", "--apply", "--json", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    (bak,) = h.glob("decision_memory.db.bak-rename-*")
    assert sorted(t for (t,) in sqlite3.connect(bak).execute("SELECT content FROM notes")) == sorted(texts)
    s = DecisionStore(str(h / "hermind.db"))
    try:
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.conn.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0] == 3
    finally:
        s.conn.close()
