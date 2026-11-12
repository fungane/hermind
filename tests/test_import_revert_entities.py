"""SPEC-2 M4: a revert also takes back what the batch taught the entity index (entities, entity_links)."""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402

NINE = ("notes", "postings", "note_entities", "note_links", "vectors", "tombstones", "learn_pending", "entities", "entity_links")
EXISTING = "Priya reviews the Zephyr dashboard together with Orchid every Monday."
GONE = "The Quasar gateway was retired after the Helios audit."
BATCH = [
    "Priya moved the Zephyr alerts to PostgreSQL on Falcon.",
    "Falcon and Nebula share the Marlin rack in building Delta.",
    "Nebula backups for Marlin run at night; Tundra keeps a copy.",
]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)
    import_core.readers()
    monkeypatch.setattr(import_core, "_READERS", dict(import_core._READERS))


def dump(db, tables=NINE):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for t in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            out[t] = con.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}").fetchall()
        return out
    finally:
        con.close()


def seed(db):
    s = DecisionStore(str(db))
    s.add(EXISTING, "state")
    s.forget(s.add(GONE, "state")[0])             # forget() leaves entity rows behind: they were there before the import
    s.close()


def source(tmp_path, texts):
    import_core.register(Reader("fake", "entries (tests)", lambda p: 3 if p.name == "fake.src" else 0,
                                lambda src, view: ReadResult([Entry(t, f"t#{i}") for i, t in enumerate(texts, 1)], [])))
    (tmp_path / "fake.src").write_text("x")
    return str(tmp_path / "fake.src")


def count(db, table):
    return sqlite3.connect(db).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_import_then_revert_leaves_all_nine_tables_as_they_were(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    before = dump(db)
    res = import_core.run_import(source(tmp_path, BATCH), db, apply=True, backup=False)
    assert res["created"] == 3
    assert count(db, "entities") > len(before["entities"]) and count(db, "entity_links") > len(before["entity_links"])
    assert dump(db) != before
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(out["remove"]) == 3 and out["entities_removed"] > 0
    assert dump(db) == before
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    again = import_core.run_import(source(tmp_path, BATCH), db, apply=True, backup=False)       # and a second round
    import_core.run_revert(again["batch"], db, apply=True, backup=False)
    assert dump(db) == before


def test_an_entity_a_later_note_still_uses_stays_with_its_links(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, BATCH), db, apply=True, backup=False)
    s = DecisionStore(str(db))
    later, _ = s.add("Nebula and Ravenna were moved to the new data hall last week.", "state")
    s.close()
    con = sqlite3.connect(db)
    ids = dict(con.execute("SELECT key, id FROM entities"))
    assert "nebula" in ids and "ravenna" in ids
    nebula, ravenna = ids["nebula"], ids["ravenna"]
    link = lambda a, b: con.execute("SELECT weight FROM entity_links WHERE a=? AND b=?", (min(a, b), max(a, b))).fetchone()
    assert link(nebula, ravenna) is not None
    batch_only = [k for k in ("falcon", "delta") if k in ids]
    assert batch_only
    con.close()
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(out["remove"]) == 3
    con = sqlite3.connect(db)
    keys = dict(con.execute("SELECT key, id FROM entities"))
    assert "nebula" in keys and "ravenna" in keys                                    # still used by the later note
    assert con.execute("SELECT weight FROM entity_links WHERE a=? AND b=?",
                       (min(nebula, ravenna), max(nebula, ravenna))).fetchone() is not None   # its link to the other entities
    for k in batch_only:
        assert k not in keys, k                                                       # nobody uses them any more
    users = {r[0] for r in con.execute("SELECT entity_id FROM note_entities")}
    assert users >= {nebula, ravenna}
    # no link points at an entity that is gone, and nothing is left with a zero weight
    assert con.execute("SELECT COUNT(*) FROM entity_links WHERE weight <= 0 OR a NOT IN (SELECT id FROM entities) "
                       "OR b NOT IN (SELECT id FROM entities)").fetchone()[0] == 0
    assert con.execute("SELECT df FROM entities WHERE id=?", (nebula,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM notes WHERE id=?", (later,)).fetchone()[0] == 1


def test_an_entity_somebody_gave_an_alias_stays(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, BATCH), db, apply=True, backup=False)
    con = sqlite3.connect(db)
    con.execute("UPDATE entities SET aliases='the usual rack' WHERE key='delta'")
    con.commit()
    con.close()
    import_core.run_revert(res["batch"], db, apply=True, backup=False)
    keys = {r[0] for r in sqlite3.connect(db).execute("SELECT key FROM entities")}
    assert "delta" in keys and "falcon" not in keys


def test_notes_that_stay_keep_their_entities(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, BATCH), db, apply=True, backup=False)
    s = DecisionStore(str(db))
    kept = sqlite3.connect(db).execute("SELECT note_id FROM import_batch_notes ORDER BY note_id").fetchall()[1][0]
    s.set_fields(kept, tags=["important"])                                           # edited after the import: it stays
    s.close()
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert [k["note_id"] for k in out["keep"]] == [kept] and len(out["remove"]) == 2
    con = sqlite3.connect(db)
    mine = {r[0] for r in con.execute("SELECT entity_id FROM note_entities WHERE note_id=?", (kept,))}
    assert mine and {r[0] for r in con.execute("SELECT id FROM entities")} >= mine
    assert con.execute("SELECT COUNT(*) FROM entity_links WHERE a NOT IN (SELECT id FROM entities) "
                       "OR b NOT IN (SELECT id FROM entities)").fetchone()[0] == 0
