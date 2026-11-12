"""The import core and `hermind import` (import_core.py, hermind_cli.py). A trivial in-test reader stands in for a source."""

import json
import shutil
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
import store_writes  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402

LIVE = "The office printer lives on the second floor next to the kitchen."
FORGOTTEN = "The old parking gate code was changed after the audit."
NORMAL1 = "Maria prefers vegetarian lunches on team days."
NORMAL2 = "The Aurora project ships its first beta on 2027-02-01."
SECRET = "deploy key is " + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
TOO_LONG = ("lorem ipsum dolor " * 6000)[:100_500]
PRIVATE = "<private>my sister's phone number is 555-0100</private>"
TABLES = ("notes", "postings", "note_entities", "note_links", "vectors", "tombstones", "learn_pending")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("HERMIND_DB_KEY", raising=False)
    monkeypatch.delenv("HERMIND_DB_KEY_FILE", raising=False)
    monkeypatch.delenv("HERMIND_DB", raising=False)
    import_core.readers()                    # load the built-in readers before the registry is copied and restored
    monkeypatch.setattr(import_core, "_READERS", dict(import_core._READERS))


def dump(db, tables=None):
    """Every row of every table (or of *tables*), sorted, as plain Python values."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = tables or [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in names:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            out[t] = con.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}").fetchall()
        return out
    finally:
        con.close()


def tables_of(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def seed(path):
    s = DecisionStore(str(path))
    live, _ = s.add(LIVE, "state", "printer office")
    gone, _ = s.add(FORGOTTEN, "state")
    s.forget(gone)
    s.close()
    return live


def source(tmp_path, entries, remarks=(), name="fake.src"):
    """Register a reader for a list of entries and return a source path it recognises."""
    import_core.register(Reader("fake", "a list of entries (tests)", lambda p: 3 if p.name == name else 0,
                                lambda src, view: ReadResult(list(entries), list(remarks))))
    p = tmp_path / name
    p.write_text("x")
    return str(p)


def nine():
    return [Entry(NORMAL1, "a.txt#1"), Entry("  " + NORMAL1.upper() + "  ", "a.txt#2"), Entry(SECRET, "a.txt#3"),
            Entry(TOO_LONG, "a.txt#4"), Entry(FORGOTTEN, "a.txt#5"), Entry("   ", "a.txt#6"),
            Entry(NORMAL2, "a.txt#7"), Entry(LIVE, "a.txt#8"), Entry(PRIVATE, "a.txt#9")]


def live_notes(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


# -- M1 check 1: the mixed set -------------------------------------------------------------------------------------

def test_mixed_set_preview_and_apply_agree_and_leave_old_notes_alone(tmp_path):
    db = tmp_path / "m.db"
    live_id = seed(db)
    scratch = tmp_path / "scratch.db"
    shutil.copy(db, scratch)
    before = [dict(r) for r in DecisionStore(str(db)).conn.execute("SELECT * FROM notes WHERE id=?", (live_id,))][0]
    src = source(tmp_path, nine())
    prev = import_core.run_import(src, db)
    app = import_core.run_import(src, db, apply=True, backup=False)
    want = {"new": 2, "version": 0, "duplicate": 2, "edited": 0, "history": 0, "refused": 4, "unreadable": 0, "empty": 1, "excluded": 0}
    assert prev["counts"] == want and app["counts"] == want and prev["total"] == app["total"] == 9
    key = lambda r: [(e["n"], e["origin"], e["outcome"], e["reason"]) for e in r["entries"]]
    assert key(prev) == key(app)
    # the refusals carry the store's own words: ask DecisionStore.add for the same texts on a scratch copy
    s2 = DecisionStore(str(scratch))
    for text, origin in ((SECRET, "a.txt#3"), (TOO_LONG, "a.txt#4"), (FORGOTTEN, "a.txt#5"), (PRIVATE, "a.txt#9")):
        with pytest.raises(store_writes.RefusedWrite) as exc:
            s2.add(text)
        got = next(e for e in app["entries"] if e["origin"] == origin)
        assert got["outcome"] == "refused" and got["reason"] == str(exc.value)
    s2.close()
    by = {e["origin"]: e for e in app["entries"]}
    assert by["a.txt#2"]["reason"].endswith("a.txt#1 earlier in this import")
    assert by["a.txt#8"]["note_id"] == live_id
    notes = live_notes(db)
    assert len(notes) == 3
    after = next(n for n in notes if n["id"] == live_id)
    assert after == before                                    # every column, proof_count included
    assert app["created"] == 2 and app["batch"]


def test_refused_text_is_never_echoed_in_the_preview(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, nine()), db)
    blob = json.dumps(res) + import_core_text(res)
    assert "k_live_" not in blob and "555-0100" not in blob


def import_core_text(res):
    return hermind_cli.import_text(res)


def test_near_duplicates_are_new(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, [Entry(LIVE + " Also the scanner.", "x#1")]), db)
    assert res["counts"]["new"] == 1


def test_not_live_duplicates_say_so(tmp_path):
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    nid, _ = s.add("Superseded fact about the build server.")
    s.conn.execute("UPDATE notes SET status='superseded' WHERE id=?", (nid,))
    s.conn.commit()
    s.close()
    res = import_core.run_import(source(tmp_path, [Entry("Superseded fact about the build server.", "x#1")]), db)
    assert res["counts"]["duplicate"] == 1 and "not live: superseded" in res["entries"][0]["reason"]


def test_excluded_and_remarks(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, [Entry(NORMAL1, "old/a.md"), Entry(NORMAL2, "new/b.md"), Entry("", "old/c.md")],
                 remarks=["3 conversations were not imported: conversation text is not memory"])
    res = import_core.run_import(src, db, exclude=["old/*"])
    assert res["counts"]["excluded"] == 2 and res["counts"]["new"] == 1 and res["counts"]["empty"] == 0
    assert res["remarks"] == ["3 conversations were not imported: conversation text is not memory"]
    assert "3 conversations were not imported" in import_core_text(res)


# -- M1 check 2: a preview writes nothing ----------------------------------------------------------------------------

def test_preview_writes_nothing_on_a_database_that_never_saw_an_import(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    before_tables, before_files = dump(db), sorted(p.name for p in tmp_path.iterdir())
    src = source(tmp_path, nine())
    before_files = sorted(p.name for p in tmp_path.iterdir())
    import_core.run_import(src, db)
    import_core.run_list(db)
    assert dump(db) == before_tables
    assert not tables_of(db) & {"import_batches", "import_batch_notes"}
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(("-wal", "-shm"))) == \
        [n for n in before_files if not n.endswith(("-wal", "-shm"))]


def test_preview_does_not_create_a_missing_database(tmp_path):
    db = tmp_path / "sub" / "none.db"
    res = import_core.run_import(source(tmp_path, [Entry(NORMAL1, "a#1")]), db)
    assert res["counts"]["new"] == 1 and not db.exists() and not db.parent.exists()
    assert import_core.run_list(db)["batches"] == []


def test_preview_does_not_upgrade_or_reindex_an_old_database(tmp_path):
    db = tmp_path / "old.db"
    seed(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("DELETE FROM meta WHERE key='tokenizer_version'")
    con.commit()
    con.close()
    before = dump(db)
    import_core.run_import(source(tmp_path, nine()), db)
    assert dump(db) == before
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 4
    assert not list(tmp_path.glob("*.bak-*"))


# -- M1 check 3: one decision function -------------------------------------------------------------------------------

def test_preview_and_apply_use_one_decision_function_and_the_stores_rules(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    seed(db)
    calls = []
    real = import_core.decide
    monkeypatch.setattr(import_core, "decide", lambda *a, **k: calls.append(1) or real(*a, **k))
    src = source(tmp_path, nine())
    import_core.run_import(src, db)
    import_core.run_import(src, db, apply=True, backup=False)
    assert len(calls) == 2
    assert import_core.check_text is store_writes.check_text           # the store's rule, not a copy
    sentinel = []
    monkeypatch.setattr(store_writes, "check_text", lambda c, k="", **kw: sentinel.append(1) or (c, k))
    s = DecisionStore(str(tmp_path / "x.db"))
    s.add("anything at all")
    s.close()
    assert sentinel                                                     # DecisionStore.add asks the same function


# -- M1 checks 4-6: revert ------------------------------------------------------------------------------------------

def test_import_revert_import_again(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, nine())
    before = dump(db, TABLES)
    first = import_core.run_import(src, db, apply=True, backup=False)
    assert first["created"] == 2
    prev = import_core.run_revert(first["batch"], db)
    assert prev["mode"] == "preview" and len(prev["remove"]) == 2 and len(live_notes(db)) == 3
    tomb = len(dump(db, ["tombstones"])["tombstones"])
    rev = import_core.run_revert(first["batch"], db, apply=True, backup=False)
    assert len(rev["remove"]) == 2 and rev["keep"] == []
    assert dump(db, TABLES) == before
    assert len(dump(db, ["tombstones"])["tombstones"]) == tomb
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    second = import_core.run_import(src, db, apply=True, backup=False)
    assert second["created"] == 2 and second["counts"]["new"] == 2 and second["batch"] != first["batch"]
    assert len(live_notes(db)) == 3
    assert [b["state"] for b in import_core.run_list(db)["batches"]] == ["reverted", "complete"]


def test_revert_keeps_notes_changed_since_the_import(tmp_path):
    db = tmp_path / "m.db"
    texts = ["Edited note about the quarterly budget review.", "Merged note about the warehouse inventory count.",
             "Superseded note about the old login page layout.", "Pinned note about the emergency contact list.",
             "Untouched note about the garden watering schedule.", "Reproved note about the vendor contract renewal.",
             "Resaved note about the mailing list archive."]
    src = source(tmp_path, [Entry(t, f"f#{i}") for i, t in enumerate(texts, 1)])
    res = import_core.run_import(src, db, apply=True, backup=False)
    assert res["created"] == 7
    ids = {e["origin"]: e["note_id"] for e in res["entries"]}
    s = DecisionStore(str(db))
    s.update(ids["f#1"], "Edited note about the quarterly budget review, now with numbers 42.")
    s.conn.execute("UPDATE notes SET status='merged', canonical_id=? WHERE id=?", (ids["f#5"], ids["f#2"]))
    s.conn.execute("UPDATE notes SET status='superseded', canonical_id=? WHERE id=?", (ids["f#5"], ids["f#3"]))
    s.conn.execute("UPDATE notes SET pinned=1 WHERE id=?", (ids["f#4"],))
    s.set_fields(ids["f#6"], tags="vendors")
    s.add(texts[6])                                                       # saved again: proof_count goes up
    s.conn.commit()
    s.close()
    prev = import_core.run_revert(res["batch"], db)
    kept = {k["origin"]: k["reason"] for k in prev["keep"]}
    assert set(kept) == {"f#1", "f#2", "f#3", "f#4", "f#6", "f#7"}
    assert "edited" in kept["f#1"] and "merged" in kept["f#2"] and "superseded" in kept["f#3"]
    assert "pinned" in kept["f#4"] and "tags" in kept["f#6"] and "saved again" in kept["f#7"]
    assert [r["origin"] for r in prev["remove"]] == ["f#5"]
    before = dump(db)
    assert import_core.run_revert(res["batch"], db)["keep"] == prev["keep"] and dump(db) == before   # preview = no change
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(out["keep"]) == 6 and len(out["remove"]) == 1
    left = {r["id"] for r in DecisionStore(str(db)).conn.execute("SELECT id FROM notes WHERE status IN ('active','merged','superseded')")}
    assert ids["f#5"] not in left and {ids["f#1"], ids["f#2"], ids["f#4"]} <= left
    assert import_core.run_list(db)["batches"][0]["state"] == "partly reverted"


def test_interrupted_apply_is_listed_incomplete_and_reverts_completely(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, [Entry(f"Imported fact number {i} about the harbour crane schedule.", f"f#{i}") for i in range(1, 6)])
    real, n = DecisionStore.add, []

    def flaky(self, *a, **k):
        n.append(1)
        if len(n) == 4:
            raise OSError("disk went away")
        return real(self, *a, **k)

    monkeypatch.setattr(DecisionStore, "add", flaky)
    res = import_core.run_import(src, db, apply=True, backup=False, chunk=2)
    monkeypatch.setattr(DecisionStore, "add", real)
    assert res["ok"] is False and "disk went away" in res["error"] and res["batch"]
    batch = import_core.run_list(db)["batches"][0]
    assert batch["state"] == "incomplete" and batch["notes_remaining"] == 2      # the first chunk was committed
    assert len(live_notes(db)) == 3
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(out["remove"]) == 2 and out["keep"] == []
    assert len(live_notes(db)) == 1
    assert import_core.run_list(db)["batches"][0]["state"] == "reverted"


# -- M1 check 7: never expires -----------------------------------------------------------------------------------------

def test_old_chat_entry_never_expires_and_is_searchable(tmp_path):
    import textnorm
    text = "We chatted about the lunch menu and everyone liked the soup."
    assert textnorm.classify_kind(text) == "chat"
    db = tmp_path / "m.db"
    seed(db)
    created = time.time() - 100 * 86400
    res = import_core.run_import(source(tmp_path, [Entry(text, "c#1", created=created)]), db, apply=True, backup=False)
    nid = res["entries"][0]["note_id"]
    s = DecisionStore(str(db))
    row = dict(s.conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone())
    assert row["expires_at"] is None and row["kind"] == "chat" and abs(row["created_at"] - created) < 1
    assert row["status"] == "active" and row["review"] == "active" and row["candidate"] == 0
    assert row["source_session"] == "import:fake" and f"import-{res['batch']}" in row["tags"].split()
    assert nid in [h["id"] for h in s.search("lunch menu soup")]
    # no later path gives it a TTL: maintenance (purge + hot-note TTL refresh), full consolidation, access tracking,
    # an update of the text, a review round trip, a restore
    s.search("lunch menu soup")
    s.flush_access(time.time())
    s.conn.execute("UPDATE notes SET last_access=? WHERE id=?", (time.time(), nid))
    s.conn.commit()
    s.maintenance(now=time.time() + 400 * 86400)
    s.consolidate(full=True)
    s.set_fields(nid, kind="news")
    s.set_fields(nid, kind="chat")
    s.update(nid, text + " Dessert was fruit.")
    s.mark_pending(nid)
    s.confirm(nid)
    s.maintenance(now=time.time() + 800 * 86400)
    after = dict(s.conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone())
    s.close()
    assert after["expires_at"] is None


# -- M1 check 8: backup first ----------------------------------------------------------------------------------------

def test_apply_backs_up_first_and_prints_the_path(tmp_path, capsys):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, nine())
    res = import_core.run_import(src, db, apply=True)
    bak = Path(res["backup"])
    assert bak.exists() and bak.name.startswith("m.db.bak-import-")
    if sys.platform != "win32":                                                   # Windows has no Unix permission bits (a new file reports 0o666)
        assert (bak.stat().st_mode & 0o777) == 0o600
    assert str(bak) in hermind_cli.import_text(res)
    old = sqlite3.connect(bak)
    assert old.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1          # the state before the first write
    assert "import_batches" not in {r[0] for r in old.execute("SELECT name FROM sqlite_master")}
    old.close()
    rv = import_core.run_revert(res["batch"], db, apply=True)
    assert rv["backup"] and Path(rv["backup"]).exists() and rv["backup"] != res["backup"]
    assert sqlite3.connect(rv["backup"]).execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3


def test_no_backup_skips_it_and_a_new_database_needs_none(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    res = import_core.run_import(source(tmp_path, nine()), db, apply=True, backup=False)
    assert res["backup"] is None and not list(tmp_path.glob("*.bak-*"))
    fresh = tmp_path / "fresh" / "n.db"
    res = import_core.run_import(source(tmp_path, [Entry(NORMAL1, "a#1")], name="g.src"), fresh, apply=True)
    assert res["created"] == 1 and res["backup"] is None and "did not exist" in res["backup_note"]


def test_nothing_to_import_writes_nothing_and_makes_no_backup(tmp_path):
    db = tmp_path / "m.db"
    seed(db)
    before = dump(db)
    res = import_core.run_import(source(tmp_path, [Entry(LIVE, "a#1")]), db, apply=True)
    assert res["batch"] is None and res["backup"] is None and dump(db) == before and not list(tmp_path.glob("*.bak-*"))


def test_encrypted_database_works_the_same_way(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    db = tmp_path / "enc.db"
    s = DecisionStore(str(db), key="correct horse")
    s.add(LIVE, "state")
    s.forget(s.add(FORGOTTEN, "state")[0])
    s.close()
    src = source(tmp_path, nine())
    with pytest.raises(RuntimeError, match="encrypted"):
        import_core.run_import(src, db)                                          # no key: a readable message
    monkeypatch.setenv("HERMIND_DB_KEY", "correct horse")
    prev = import_core.run_import(src, db)
    assert prev["counts"]["new"] == 2
    res = import_core.run_import(src, db, apply=True)
    bak = Path(res["backup"])
    assert bak.exists() and not bak.read_bytes().startswith(b"SQLite format 3")
    s = DecisionStore(str(bak), key="correct horse")
    assert s.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    s.close()
    rv = import_core.run_revert(res["batch"], db, apply=True)
    assert len(rv["remove"]) == 2 and rv["backup"]
    assert import_core.run_list(db)["batches"][0]["state"] == "reverted"


# -- M1 check 9: polite writes ------------------------------------------------------------------------------------------

def test_large_runs_commit_in_chunks(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    commits = []
    real = import_core._open_store

    def spy(path, key):
        st = real(path, key)
        st.conn.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
        return st

    monkeypatch.setattr(import_core, "_open_store", spy)
    entries = [Entry(f"Harbour log entry {i}: crane {i % 7} moved container {i * 13} to bay {i % 11}.", f"log#{i}")
               for i in range(1, 651)]
    res = import_core.run_import(source(tmp_path, entries), db, apply=True, backup=False)
    assert res["created"] == 650 and import_core.CHUNK <= 500
    assert 3 <= len(commits) <= 8                    # 650 notes are never one giant transaction, nor one per note


# -- M1 check 10: the command line ------------------------------------------------------------------------------------

def cli(*args, capsys):
    code = hermind_cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_preview_apply_list_revert_json(tmp_path, capsys):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, nine())
    code, out, _ = cli("import", src, "--db", str(db), "--json", capsys=capsys)
    d = json.loads(out)
    assert code == 0 and d["mode"] == "preview" and d["reader"] == "fake" and d["reader_detected"] is True
    assert set(d) >= {"ok", "mode", "database", "source", "reader", "counts", "total", "entries", "remarks", "batch", "created", "backup"}
    assert set(d["entries"][0]) >= {"n", "origin", "outcome", "reason"}
    code, out, _ = cli("import", src, "--db", str(db), capsys=capsys)
    assert code == 0 and "PREVIEW" in out and "chosen from the shape" in out and "a.txt#3" in out
    code, out, _ = cli("import", src, "--from", "fake", "--apply", "--db", str(db), "--json", capsys=capsys)
    applied = json.loads(out)
    assert code == 0 and applied["created"] == 2 and applied["backup"] and applied["reader_detected"] is False
    code, out, _ = cli("import", "--list", "--db", str(db), "--json", capsys=capsys)
    b = json.loads(out)["batches"]
    assert code == 0 and b[0]["batch"] == applied["batch"] and b[0]["state"] == "complete" and b[0]["notes_created"] == 2
    assert set(b[0]) >= {"batch", "reader", "source", "created", "counts", "state", "notes_created", "notes_remaining"}
    code, out, _ = cli("import", "--list", "--db", str(db), capsys=capsys)
    assert applied["batch"] in out and "complete" in out
    code, out, _ = cli("import", "--revert", applied["batch"], "--db", str(db), "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["mode"] == "preview" and len(live_notes(db)) == 3
    code, out, _ = cli("import", "--revert", applied["batch"], "--apply", "--db", str(db), capsys=capsys)
    assert code == 0 and "REVERTED" in out and len(live_notes(db)) == 1


def test_cli_exclude_readers_and_errors(tmp_path, capsys):
    db = tmp_path / "m.db"
    seed(db)
    src = source(tmp_path, [Entry(NORMAL1, "keep/a#1"), Entry(NORMAL2, "skip/b#1"), Entry("Third unrelated fact.", "skip/c#1")])
    code, out, _ = cli("import", src, "--exclude", "skip/*", "--exclude", "keep/zzz", "--db", str(db), "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["counts"]["excluded"] == 2
    code, out, _ = cli("import", "--readers", "--json", capsys=capsys)
    assert code == 0 and any(r["name"] == "fake" and r["accepts"] for r in json.loads(out)["readers"])
    # real errors exit non-zero
    assert cli("import", str(tmp_path / "missing"), "--db", str(db), capsys=capsys)[0] == 2
    assert cli("import", src, "--from", "nope", "--db", str(db), capsys=capsys)[0] == 2
    assert cli("import", "--revert", "20200101-000000-0000", "--db", str(db), capsys=capsys)[0] == 2
    assert cli("import", "--db", str(db), capsys=capsys)[0] == 2
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database at all" * 10)
    code, out, _ = cli("import", src, "--db", str(junk), capsys=capsys)
    assert code == 2 and "ERROR" in out
    # outcomes are not errors: all refused is still exit 0
    only_bad = source(tmp_path, [Entry(SECRET, "z#1")], name="bad.src")
    assert cli("import", only_bad, "--apply", "--db", str(db), capsys=capsys)[0] == 0


def test_cli_ambiguous_source_asks_for_from(tmp_path, capsys):
    db = tmp_path / "m.db"
    for name in ("one", "two"):
        import_core.register(Reader(name, "t", lambda p: 2, lambda s, v: ReadResult([Entry("A fact.", "a#1")])))
    src = tmp_path / "x.src"
    src.write_text("x")
    code, out, _ = cli("import", str(src), "--db", str(db), capsys=capsys)
    assert code == 2 and "--from" in out
    assert cli("import", str(src), "--from", "two", "--db", str(db), capsys=capsys)[0] == 0
    assert cli("import", str(tmp_path / "x.src"), "--from", "one", "--db", str(db), "--apply", capsys=capsys)[0] == 0


def test_import_help_explains_the_modes_in_plain_words(capsys):
    with pytest.raises(SystemExit):
        hermind_cli.main(["import", "--help"])
    out = capsys.readouterr().out
    for word in ("PREVIEW", "--apply", "--list", "--revert", "--readers", "backup"):
        assert word in out
