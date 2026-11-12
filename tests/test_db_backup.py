"""The copy made before a schema upgrade (db_backup.py, built on SQLite's online backup).

It holds the newest notes even while another connection is in the middle of a read, waits neither for that reader nor
for a writer, keeps an encrypted database encrypted, is owner-only, leaves nothing behind when it fails (and then the
upgrade is refused), and keeps the newest copies in the order they were made, old-style names included.
"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db_backup  # noqa: E402
import store_schema  # noqa: E402
from store import DecisionStore  # noqa: E402

BASE = "The office printer lives on the second floor next to the kitchen."
LATE = "A note saved after the reader had already started its transaction."
KEY = "correct horse battery staple"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(v, raising=False)


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


def user_version(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def old_db(path, key=None, version=4):
    """A database that an older Hermind wrote (schema *version*), holding one note."""
    s = DecisionStore(str(path), key=key)
    s.add(BASE, "state")
    s.conn.execute(f"PRAGMA user_version={version}")
    s.conn.commit()
    s.conn.close()


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


# -- the bug this guards against: a checkpoint stops at an open reader, so a copy of the main file missed the newest note

def test_upgrade_backup_holds_the_newest_note_while_another_connection_is_reading(tmp_path):
    db = tmp_path / "m.db"
    old_db(db)
    reader = reader_then_writer(db)
    t0 = time.time()
    try:
        DecisionStore(str(db)).conn.close()
    finally:
        took = time.time() - t0
        reader.execute("ROLLBACK")
        reader.close()
    (bak,) = tmp_path.glob("m.db.bak-v4-*")
    assert notes_in(bak) == [BASE, LATE]
    assert user_version(bak) == 4                                     # the copy is still the pre-upgrade state
    assert took < 2.5, took                                           # the old copy first waited the 5 s busy timeout


def test_the_copy_leaves_no_sidecar_files(tmp_path):
    db = tmp_path / "m.db"
    old_db(db)
    DecisionStore(str(db)).conn.close()
    assert [p.name for p in tmp_path.glob("m.db.bak-*")] == [next(tmp_path.glob("m.db.bak-v4-*")).name]
    assert not [p for p in tmp_path.glob("m.db.bak-*") if p.name.endswith(("-wal", "-shm", "-journal"))]


# -- encrypted databases ------------------------------------------------------------------------------------------

def test_encrypted_upgrade_backup_stays_encrypted(tmp_path):
    pytest.importorskip("sqlcipher3")
    db = tmp_path / "e.db"
    old_db(db, key=KEY)
    DecisionStore(str(db), key=KEY).conn.close()
    (bak,) = tmp_path.glob("e.db.bak-v4-*")
    assert notes_in(bak, KEY) == [BASE]
    with pytest.raises(sqlite3.DatabaseError):                        # plain sqlite cannot read it: still encrypted
        sqlite3.connect(str(bak)).execute("SELECT * FROM notes").fetchall()


def test_encrypted_upgrade_backup_with_the_key_from_the_environment(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    db = tmp_path / "e.db"
    old_db(db, key=KEY)
    monkeypatch.setenv("HERMIND_DB_KEY", KEY)
    DecisionStore(str(db)).conn.close()
    (bak,) = tmp_path.glob("e.db.bak-v4-*")
    assert notes_in(bak, KEY) == [BASE]


# -- no backup, no upgrade ----------------------------------------------------------------------------------------

def _fail_partway(monkeypatch):
    def broken(src, dst):
        dst.execute("CREATE TABLE half(x)")
        dst.commit()
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db_backup, "_copy", broken)


def test_a_copy_that_fails_partway_leaves_nothing_and_the_upgrade_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    old_db(db)
    _fail_partway(monkeypatch)
    with pytest.raises(RuntimeError, match="could not back it up"):
        DecisionStore(str(db))
    assert not list(tmp_path.glob("m.db.bak-*"))
    assert user_version(db) == 4


def test_a_copy_that_fails_its_integrity_check_is_discarded_and_the_upgrade_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    old_db(db)
    monkeypatch.setattr(db_backup, "_integrity", lambda conn: "row 1 missing from index notes_hash")
    with pytest.raises(RuntimeError, match="integrity"):
        DecisionStore(str(db))
    assert not list(tmp_path.glob("m.db.bak-*"))
    assert user_version(db) == 4


def test_no_copy_at_all_also_refuses_the_upgrade(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    old_db(db)
    monkeypatch.setattr(store_schema, "backup_database", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="could not back it up"):
        DecisionStore(str(db))
    assert user_version(db) == 4


# -- which copies stay --------------------------------------------------------------------------------------------

def test_old_style_names_are_pruned_in_the_order_made_and_the_new_copy_stays(tmp_path):
    db = tmp_path / "m.db"
    old_db(db)
    for day in range(1, 5):                                           # copies made by earlier versions: no counter
        (tmp_path / f"m.db.bak-v3-2020010{day}-000000").write_bytes(b"x")
    DecisionStore(str(db)).conn.close()
    left = sorted(p.name for p in tmp_path.glob("m.db.bak-v*"))
    assert len(left) == 3
    assert left[:2] == ["m.db.bak-v3-20200103-000000", "m.db.bak-v3-20200104-000000"]
    assert left[2].startswith("m.db.bak-v4-")


def copy_of(db, **kw) -> Path:
    b = db_backup.backup_database(db, **kw)
    assert b is not None
    return b.path


def test_names_sort_in_the_order_made_also_within_one_second(tmp_path):
    db = tmp_path / "m.db"
    old_db(db)
    made = [copy_of(db, prefix="bak-test", keep=100, now=1_800_000_000.0) for _ in range(12)]
    assert sorted(made) == made and len(set(made)) == 12


def test_the_copy_does_not_wait_for_a_writer_and_holds_only_committed_notes(tmp_path):
    db = tmp_path / "m.db"
    old_db(db)
    w = sqlite3.connect(str(db), isolation_level=None)
    w.execute("BEGIN IMMEDIATE")
    w.execute("INSERT INTO notes(content, created_at) VALUES ('uncommitted', 1)")
    t0 = time.time()
    try:
        path = copy_of(db, prefix="bak-test")
    finally:
        took = time.time() - t0
        w.execute("ROLLBACK")
        w.close()
    assert took < 2.5, took
    assert notes_in(path) == [BASE]                                   # a consistent snapshot of committed notes


def test_no_database_file_means_no_copy(tmp_path):
    assert db_backup.backup_database(tmp_path / "nothing.db", prefix="bak-test") is None
