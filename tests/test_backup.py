"""One backup routine (db_backup.py) for imports, reverts and the schema upgrade: it holds the newest notes even while a
reader has a transaction open, keeps encryption, is owner-only, leaves nothing behind when it fails, and keeps the newest
copies in the order they were made."""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402

BASE = "The office printer lives on the second floor next to the kitchen."
LATE = "A note saved after the reader had already started its transaction."
KEY = "correct horse battery staple"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(v, raising=False)
    import_core.readers()
    monkeypatch.setattr(import_core, "_READERS", dict(import_core._READERS))


def source(tmp_path, texts):
    import_core.register(Reader("fake", "a list of entries (tests)", lambda p: 3 if p.name == "fake.src" else 0,
                                lambda src, view: ReadResult([Entry(t, f"t#{i}") for i, t in enumerate(texts, 1)], [])))
    p = tmp_path / "fake.src"
    p.write_text("x")
    return str(p)


def notes_in(path, key=None):
    if key is None:
        con = sqlite3.connect(str(path))
    else:
        import dbdriver
        con = dbdriver.connect(str(path), key, use_env=False)
    try:
        return [r[0] for r in con.execute("SELECT content FROM notes ORDER BY id")]
    finally:
        con.close()


def reader_then_writer(db):
    """A reader opens a read transaction and reads; then another connection saves a note. Returns the reader."""
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM notes").fetchone()
    w = sqlite3.connect(str(db))
    w.execute("INSERT INTO notes(content, created_at) VALUES (?, ?)", (LATE, time.time()))
    w.commit()
    w.close()
    return reader


# -- check 1: a backup holds the newest notes even with a reader in the middle of a read --------------------------

def test_import_backup_holds_the_newest_note_with_a_reader_open_and_is_quick(tmp_path):
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    s.add(BASE, "state")
    s.close()
    reader = reader_then_writer(db)
    t0 = time.time()
    res = import_core.run_import(source(tmp_path, ["Maria prefers vegetarian lunches."]), db, apply=True)
    took = time.time() - t0
    reader.execute("ROLLBACK")
    reader.close()
    assert res["ok"] and res["backup"]
    assert LATE in notes_in(res["backup"])
    assert took < 1.0, took


def test_upgrade_backup_holds_the_newest_note_with_a_reader_open_and_is_quick(tmp_path):
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    s.add(BASE, "state")
    s.conn.execute("PRAGMA user_version=4")
    s.conn.commit()
    s.close()
    reader = reader_then_writer(db)
    t0 = time.time()
    DecisionStore(str(db)).close()
    took = time.time() - t0
    reader.execute("ROLLBACK")
    reader.close()
    baks = list(tmp_path.glob("m.db.bak-v4-*"))
    assert len(baks) == 1 and LATE in notes_in(baks[0])
    assert took < 1.0, took


# -- check 2: encrypted database ------------------------------------------------------------------------------

def test_encrypted_database_backup_opens_with_the_same_key_and_has_the_newest_note(tmp_path):
    pytest.importorskip("sqlcipher3")
    db = tmp_path / "e.db"
    s = DecisionStore(str(db), key=KEY)
    s.add(BASE, "state")
    s.add(LATE, "state")
    s.close()
    res = import_core.run_import(source(tmp_path, ["Maria prefers vegetarian lunches."]), db, apply=True, key=KEY)
    assert res["ok"]
    bak = Path(res["backup"])
    assert notes_in(bak, KEY) == [BASE, LATE]
    with pytest.raises(sqlite3.DatabaseError):                       # still encrypted: plain sqlite cannot read it
        sqlite3.connect(str(bak)).execute("SELECT * FROM notes").fetchall()


def test_encrypted_upgrade_backup_stays_encrypted(tmp_path):
    pytest.importorskip("sqlcipher3")
    db = tmp_path / "e.db"
    s = DecisionStore(str(db), key=KEY)
    s.add(BASE, "state")
    s.conn.execute("PRAGMA user_version=4")
    s.conn.commit()
    s.close()
    DecisionStore(str(db), key=KEY).close()
    (bak,) = tmp_path.glob("e.db.bak-v4-*")
    assert notes_in(bak, KEY) == [BASE]


# -- check 3: owner-only; a copy that fails partway leaves nothing and stops before any write -------------------

def test_copy_is_owner_only(tmp_path):
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    res = import_core.run_import(source(tmp_path, ["Maria prefers vegetarian lunches."]), db, apply=True)
    assert (Path(res["backup"]).stat().st_mode & 0o777) == 0o600


def _fail_partway(monkeypatch):
    import db_backup

    def broken(src, dst):
        dst.execute("CREATE TABLE half(x)")
        dst.commit()
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db_backup, "_copy", broken)


def test_failed_import_backup_leaves_no_file_and_nothing_is_written(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    s.add(BASE, "state")
    s.close()
    before = db.read_bytes()
    _fail_partway(monkeypatch)
    res = import_core.run_import(source(tmp_path, ["Maria prefers vegetarian lunches."]), db, apply=True)
    assert res["ok"] is False and "backup" in res["error"] and "nothing was written" in res["error"]
    assert res["batch"] is None and res["created"] == 0
    assert not list(tmp_path.glob("m.db.bak-*"))
    assert notes_in(db) == [BASE]
    assert "import_batches" not in {r[0] for r in sqlite3.connect(str(db)).execute("SELECT name FROM sqlite_master")}
    assert db.read_bytes() == before


def test_failed_revert_backup_stops_before_anything_is_removed(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    res = import_core.run_import(source(tmp_path, ["Maria prefers vegetarian lunches."]), db, apply=True)
    _fail_partway(monkeypatch)
    with pytest.raises(import_core.ImportFailed, match="backup"):
        import_core.run_revert(res["batch"], db, apply=True)
    assert len(notes_in(db)) == 1                                    # nothing was removed
    assert len(list(tmp_path.glob("m.db.bak-*"))) == 1               # only the first import's copy


def test_failed_upgrade_backup_refuses_the_upgrade(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    s.add(BASE, "state")
    s.conn.execute("PRAGMA user_version=4")
    s.conn.commit()
    s.close()
    _fail_partway(monkeypatch)
    with pytest.raises(RuntimeError, match="could not back it up"):
        DecisionStore(str(db))
    assert not list(tmp_path.glob("m.db.bak-*"))
    assert sqlite3.connect(str(db)).execute("PRAGMA user_version").fetchone()[0] == 4


def test_a_failed_integrity_check_discards_the_copy(tmp_path, monkeypatch):
    import db_backup
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    monkeypatch.setattr(db_backup, "_integrity", lambda conn: "row 1 missing from index notes_hash")
    with pytest.raises(db_backup.BackupError, match="integrity"):
        db_backup.backup_database(db)
    assert not list(tmp_path.glob("m.db.bak-*"))


# -- check 4: the newest 5 import backups stay, in the order they were made ---------------------------------------

def test_seven_imports_keep_the_newest_five_backups_and_say_how_many_went(tmp_path):
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    removed = []
    texts = [f"Fact number {w} about the Orion project." for w in
             ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")]
    for i, t in enumerate(texts):
        res = import_core.run_import(source(tmp_path, [t]), db, apply=True)
        assert res["created"] == 1
        removed.append(res["backup_removed"])
        if res["backup_removed"]:
            assert f"{res['backup_removed']} older backup" in hermind_cli.import_text(res)
    assert removed == [0, 0, 0, 0, 0, 1, 1]
    baks = sorted(tmp_path.glob("m.db.bak-import-*"))
    assert len(baks) == 5
    # sorted by name = order made: the copy before import n holds n-1 notes, so the kept ones hold 2..6
    assert [len(notes_in(b)) for b in baks] == [2, 3, 4, 5, 6]


def test_names_sort_in_the_order_made_also_within_one_second(tmp_path):
    import db_backup
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    made = [db_backup.backup_database(db, keep=100, now=1_800_000_000.0).path for _ in range(12)]
    assert sorted(made) == made and len(set(made)) == 12


def test_prune_never_deletes_the_new_copy_and_ignores_other_kinds(tmp_path):
    import db_backup
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    other = tmp_path / "m.db.bak-v3-20200101-000000"
    other.write_bytes(b"x")
    for _ in range(4):
        b = db_backup.backup_database(db, keep=2)
    assert b.path.exists() and other.exists()
    assert len(list(tmp_path.glob("m.db.bak-import-*"))) == 2


def test_no_database_file_means_no_backup(tmp_path):
    import db_backup
    assert db_backup.backup_database(tmp_path / "nothing.db") is None


def test_the_backup_does_not_wait_for_a_writer_holding_the_lock(tmp_path):
    import db_backup
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    s.add(BASE, "state")
    s.close()
    w = sqlite3.connect(str(db), isolation_level=None)
    w.execute("BEGIN IMMEDIATE")
    w.execute("INSERT INTO notes(content, created_at) VALUES ('uncommitted', 1)")
    t0 = time.time()
    b = db_backup.backup_database(db)
    assert time.time() - t0 < 1.0 and notes_in(b.path) == [BASE]       # only committed notes: a consistent snapshot
    w.execute("ROLLBACK")
    w.close()
