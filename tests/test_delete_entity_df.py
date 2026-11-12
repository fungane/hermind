"""SPEC-4 M5: deleting several notes that share an entity lowers the entity's df by the number of deleted notes."""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402


def df(store, key):
    return store.conn.execute("SELECT df FROM entities WHERE key=?", (key,)).fetchone()[0]


def links(store, key):
    return store.conn.execute("SELECT COUNT(*) FROM note_entities ne JOIN entities e ON e.id=ne.entity_id WHERE e.key=?",
                              (key,)).fetchone()[0]


def test_deleting_three_notes_that_share_an_entity_lowers_df_by_three(tmp_path):
    s = DecisionStore(str(tmp_path / "m.db"))
    try:
        ids = [s.add(f"Falcon runs job {n} for the Marlin rack.", entities=["Falcon"])[0] for n in range(5)]
        assert df(s, "falcon") == 5
        with s.conn:
            s._delete_ids(ids[:3])
        assert df(s, "falcon") == 2 == links(s, "falcon")
        assert s.forget(ids[3]) and df(s, "falcon") == 1 == links(s, "falcon")
    finally:
        s.close()


def test_the_count_is_right_across_the_chunks_of_500(tmp_path):
    s = DecisionStore(str(tmp_path / "m.db"))
    try:
        with s.conn:
            ids = [s.add(f"Falcon handled request number {n}.", entities=["Falcon"], commit=False)[0] for n in range(1100)]
        assert df(s, "falcon") == 1100
        with s.conn:
            s._delete_ids(ids[:1050])
        assert df(s, "falcon") == 50 == links(s, "falcon")
    finally:
        s.close()


def test_after_an_import_and_a_revert_every_entitys_df_equals_its_note_entities_rows(tmp_path):
    texts = ["Priya moved the Zephyr alerts to PostgreSQL on Falcon.", "Falcon and Nebula share the Marlin rack in Delta.",
             "Nebula backups for Marlin run at night; Tundra keeps a copy.", "Zephyr and Falcon alerts go to Tundra."]
    src = tmp_path / "x"
    src.write_text("")
    import_core.register(Reader("fake", "test", lambda p: 0, lambda p, v: ReadResult([Entry(t, f"e#{i}") for i, t in enumerate(texts)])))
    try:
        db = tmp_path / "m.db"
        s = DecisionStore(str(db))
        s.add("Orchid review is on Monday with Falcon.", entities=["Falcon", "Orchid"])
        s.close()
        out = import_core.run_import(str(src), db, reader="fake", apply=True, backup=False)
        assert out["created"] == 4

        def bad():
            con = sqlite3.connect(db)
            try:
                return con.execute("SELECT e.key FROM entities e WHERE e.df != (SELECT COUNT(*) FROM note_entities n "
                                   "WHERE n.entity_id = e.id)").fetchall()
            finally:
                con.close()
        assert bad() == []
        import_core.run_revert(out["batch"], db, apply=True, backup=False)
        assert bad() == []
        s = DecisionStore(str(db))
        assert df(s, "falcon") == 1 == links(s, "falcon")
        s.close()
    finally:
        import_core._READERS.pop("fake", None)
