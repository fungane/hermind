"""The Hermes built-in memory reader of `hermind import` (MEMORY.md / USER.md, entries separated by a line with only §)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
from store import DecisionStore  # noqa: E402

USER = ("User prefers short replies with bullet points.\n§\nUser writes in Cantonese and English.\n§\n"
        "User's timezone is Hong Kong time.\n")
MEMORY = "The VPS has 4GB RAM and runs Ubuntu 22.04.\n§\nBackups go to the second disk every night.\n"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def memdir(tmp_path):
    d = tmp_path / "memories"
    d.mkdir()
    (d / "USER.md").write_text(USER, encoding="utf-8")
    (d / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (d / "notes.log").write_text("not memory\n", encoding="utf-8")
    return d


def notes(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


# -- M3 check 1 ------------------------------------------------------------------------------------------------------------

def test_three_entries_give_three_notes_written_like_the_live_mirror(tmp_path, memdir):
    db = tmp_path / "h.db"
    prev = import_core.run_import(str(memdir / "USER.md"), db)
    assert prev["reader"] == "hermes-builtin" and prev["reader_detected"] and prev["counts"]["new"] == 3
    assert [e["origin"] for e in prev["entries"]] == ["USER.md#1", "USER.md#2", "USER.md#3"]
    assert not db.exists()
    res = import_core.run_import(str(memdir / "USER.md"), db, apply=True)
    assert res["created"] == 3
    got = notes(db)
    assert len(got) == 3
    for n in got:
        assert n["kind"] == "preference" and n["pinned"] == 1 and n["source_session"] == "builtin:user"
        assert n["expires_at"] is None and n["review"] == "active"
    assert got[1]["content"] == "User writes in Cantonese and English."


def test_a_folder_reads_both_files_and_says_it_ignored_the_rest(tmp_path, memdir):
    res = import_core.run_import(str(memdir), tmp_path / "h.db", apply=True, backup=False)
    assert res["reader"] == "hermes-builtin" and res["created"] == 5
    assert any("notes.log" in r and "ignored" in r for r in res["remarks"])
    got = {n["content"]: n for n in notes(tmp_path / "h.db")}
    assert got["The VPS has 4GB RAM and runs Ubuntu 22.04."]["kind"] == "state"
    assert got["The VPS has 4GB RAM and runs Ubuntu 22.04."]["source_session"] == "builtin:memory"
    assert sorted(e["origin"] for e in res["entries"]) == sorted(
        ["MEMORY.md#1", "MEMORY.md#2", "USER.md#1", "USER.md#2", "USER.md#3"])


def test_folder_with_other_notes_is_ambiguous_and_from_decides(tmp_path, memdir, capsys):
    (memdir / "ideas.md").write_text("A different note.\n", encoding="utf-8")
    code = hermind_cli.main(["import", str(memdir), "--db", str(tmp_path / "h.db")])
    assert code == 2 and "--from" in capsys.readouterr().out
    res = import_core.run_import(str(memdir), tmp_path / "h.db", reader="hermes-builtin")
    assert res["counts"]["new"] == 5 and any("ideas.md" in r for r in res["remarks"])


def test_separator_edge_cases_and_cleaning(tmp_path):
    f = tmp_path / "MEMORY.md"
    f.write_text("first entry\r\nsecond line\r\n§\r\n\r\n§\r\n  §  \r\nlast one with § inside it\r\n"
                 "§\r\n" + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2" + "\r\n", encoding="utf-8")
    res = import_core.run_import(str(f), tmp_path / "h.db")
    out = {e["origin"]: e["outcome"] for e in res["entries"]}
    assert out == {"MEMORY.md#1": "new", "MEMORY.md#2": "empty", "MEMORY.md#3": "empty", "MEMORY.md#4": "new",
                   "MEMORY.md#5": "refused"}
    assert res["entries"][0]["text"] == "first entry second line"      # a multi-line entry stays one note


# -- M3 check 2 ------------------------------------------------------------------------------------------------------------

def test_an_entry_the_mirror_already_stored_is_a_duplicate(tmp_path, memdir, provider):
    db = Path(provider._store.db_path)
    provider.on_memory_write("add", "user", "User prefers short replies with bullet points.")
    res = import_core.run_import(str(memdir / "USER.md"), db, apply=True, backup=False)
    by = {e["origin"]: e for e in res["entries"]}
    assert by["USER.md#1"]["outcome"] == "duplicate" and by["USER.md#1"]["note_id"]
    assert res["counts"]["new"] == 2 and res["counts"]["duplicate"] == 1 and len(notes(db)) == 3
    assert next(n for n in notes(db) if n["id"] == by["USER.md#1"]["note_id"])["proof_count"] == 1   # untouched


# -- M3 check 3 ------------------------------------------------------------------------------------------------------------

def test_replace_and_remove_through_the_builtin_tool_find_the_imported_note(tmp_path, memdir, provider):
    db = Path(provider._store.db_path)
    res = import_core.run_import(str(memdir / "USER.md"), db, apply=True, backup=False)
    ids = {e["origin"]: e["note_id"] for e in res["entries"]}
    old = "User writes in Cantonese and English."
    provider.on_memory_write("replace", "user", "User writes in Cantonese, English and some Mandarin.",
                             {"previous_content": old})
    got = {n["id"]: n for n in notes(db)}
    assert len(got) == 3 and "Mandarin" in got[ids["USER.md#2"]]["content"]          # updated in place, no second note
    hist = DecisionStore(str(db))
    assert hist.conn.execute("SELECT COUNT(*) FROM notes WHERE status='superseded'").fetchone()[0] == 1
    hist.close()
    provider.on_memory_write("remove", "user", "", {"previous_content": "User prefers short replies with bullet points."})
    left = {n["id"] for n in notes(db)}
    assert ids["USER.md#1"] not in left and len(left) == 2


# -- M3 check 4 ------------------------------------------------------------------------------------------------------------

def test_reverting_the_batch_removes_the_notes_it_created(tmp_path, memdir):
    db = tmp_path / "h.db"
    res = import_core.run_import(str(memdir), db, apply=True, backup=False)
    assert len(notes(db)) == 5
    rv = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(rv["remove"]) == 5 and rv["keep"] == [] and notes(db) == []


def test_cli_lists_the_reader(capsys):
    assert hermind_cli.main(["import", "--readers", "--json"]) == 0
    names = {r["name"]: r["accepts"] for r in json.loads(capsys.readouterr().out)["readers"]}
    assert {"markdown", "hermes-builtin"} <= set(names) and all(names.values())


def test_an_imported_note_has_the_same_columns_as_the_live_mirror_wrote(tmp_path, memdir, provider):
    mirror_db = Path(provider._store.db_path)
    provider.on_memory_write("add", "user", "User writes in Cantonese and English.")
    provider.on_memory_write("add", "memory", "The VPS has 4GB RAM and runs Ubuntu 22.04.")
    import_core.run_import(str(memdir), tmp_path / "imp.db", apply=True, backup=False)
    cols = ("content", "kind", "pinned", "source_session", "expires_at", "scope", "status", "review", "candidate")
    pick = lambda db, text: next({c: n[c] for c in cols} for n in notes(db) if n["content"] == text)
    for text in ("User writes in Cantonese and English.", "The VPS has 4GB RAM and runs Ubuntu 22.04."):
        assert pick(tmp_path / "imp.db", text) == pick(mirror_db, text)


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_linked_memory_file_in_a_folder_is_reported_not_read(tmp_path):
    import os
    outside = tmp_path / "outside.md"
    outside.write_text("Zorblax backup runs at 03:00 on node seven.\n§\nQuillon reports are due on Fridays.\n", encoding="utf-8")
    d = tmp_path / "memories"
    d.mkdir()
    (d / "USER.md").write_text(USER, encoding="utf-8")
    os.symlink(outside, d / "MEMORY.md")
    res = import_core.run_import(str(d), tmp_path / "h.db", reader="hermes-builtin", apply=True, backup=False)
    assert res["counts"]["new"] == 3 and res["counts"]["unreadable"] == 1
    bad = next(e for e in res["entries"] if e["outcome"] == "unreadable")
    assert bad["origin"] == "MEMORY.md" and "symbolic link" in bad["reason"]
    s = DecisionStore(str(tmp_path / "h.db"))
    try:
        assert not any("Zorblax" in r[0] for r in s.conn.execute("SELECT content FROM notes"))
    finally:
        s.close()
