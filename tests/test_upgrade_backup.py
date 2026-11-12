import os
import sqlite3
import stat
from pathlib import Path

import pytest

from store import DecisionStore


def _old_db(path: Path, version: int = 4) -> int:
    s = DecisionStore(str(path))
    nid, _ = s.add("Atlas uses PostgreSQL for orders", kind="decision")
    s.conn.execute(f"PRAGMA user_version={version}")
    s.conn.commit()
    s.conn.close()
    return nid


def test_upgrade_makes_an_owner_only_backup_first_and_keeps_the_data(tmp_path):
    db = tmp_path / "m.db"
    nid = _old_db(db)
    s = DecisionStore(str(db))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] >= 5
    assert s.get(nid)["content"].startswith("Atlas")
    baks = list(tmp_path.glob("m.db.bak-v4-*"))
    assert len(baks) == 1 and stat.S_IMODE(baks[0].stat().st_mode) == 0o600
    old = sqlite3.connect(str(baks[0]))
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4          # the copy is the pre-upgrade state
    assert old.execute("SELECT count(*) FROM notes").fetchone()[0] == 1


def test_fresh_or_current_database_gets_no_backup(tmp_path):
    DecisionStore(str(tmp_path / "a.db")).conn.close()
    DecisionStore(str(tmp_path / "a.db")).conn.close()
    assert not list(tmp_path.glob("*.bak-*"))


def test_only_the_newest_backups_are_kept(tmp_path):
    db = tmp_path / "m.db"
    _old_db(db)
    for i in range(5):
        (tmp_path / f"m.db.bak-v3-2026010{i}-000000").write_bytes(b"x")
    DecisionStore(str(db)).conn.close()
    assert len(list(tmp_path.glob("m.db.bak-v*"))) == 3


def test_upgrade_is_refused_when_the_backup_cannot_be_made(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _old_db(db)
    import db_backup
    monkeypatch.setattr(db_backup, "_copy", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="could not back it up"):
        DecisionStore(str(db))
    assert sqlite3.connect(str(db)).execute("PRAGMA user_version").fetchone()[0] == 4   # untouched
