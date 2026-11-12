"""The Markdown reader of `hermind import`, and the three changes to tools/import_md.py (md_notes.py, import_readers.py)."""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
import md_notes  # noqa: E402
from store import DecisionStore  # noqa: E402

NOW = time.time()
DAY = 86400.0
SECRET = "deploy key is " + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_t2", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



@pytest.fixture(scope="module")
def import_md():
    return _load("import_md")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def export(db, out, *flags):
    with contextlib.redirect_stdout(io.StringIO()):
        assert hermind_cli.main(["export", "--db", str(db), "--format", "markdown", "--out", str(out), *flags]) == 0


def tool_import(import_md, db, folder, *flags):
    buf = io.StringIO()
    code = import_md.run(import_md.build_parser().parse_args(["--db", str(db), str(folder), *flags]), out=buf)
    return code, buf.getvalue()


def three_notes(path):
    s = DecisionStore(str(path))
    a = s.add("Nimbus backups run daily at 03:00 and keep 7 days.", "tech", "nimbus backup schedule", tags="ops infra",
              created_at=NOW - 9 * DAY, valid_from="2026-09-01", valid_to="2027-09-01")[0]
    b = s.add("Always answer Priya in plain English.", "preference", pinned=True, created_at=NOW - 8 * DAY)[0]
    c = s.add("Currently the Orchid server has 40GB of disk left.", "state", "orchid disk", created_at=NOW - 2 * DAY)[0]
    s.close()
    return a, b, c


def rows(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


def dump(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            out[t] = con.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}").fetchall()
        return out
    finally:
        con.close()


# -- M2 check 1: an exported folder comes into an empty database -----------------------------------------------------------

def test_exported_folder_imports_into_an_empty_database_under_new_ids(tmp_path):
    a_db, b_db, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out"
    three_notes(a_db)
    export(a_db, out)
    prev = import_core.run_import(str(out), b_db)
    assert prev["reader"] == "markdown" and prev["reader_detected"] and prev["counts"]["new"] == 3
    assert not b_db.exists()                                               # a preview does not even create it
    res = import_core.run_import(str(out), b_db, apply=True)
    assert res["created"] == 3 and res["counts"] == {"new": 3, "version": 0, "duplicate": 0, "edited": 0, "history": 0, "refused": 0, "unreadable": 0, "empty": 0, "excluded": 0}
    assert any("index.md" in r for r in res["remarks"])
    mine, theirs = rows(b_db), rows(a_db)
    assert len(mine) == 3
    key = lambda n: n["content"]
    for new, old in zip(sorted(mine, key=key), sorted(theirs, key=key)):
        assert new["content"] == old["content"] and new["kind"] == old["kind"] and new["keywords"] == old["keywords"]
        assert set(new["tags"].split()) - {t for t in new["tags"].split() if t.startswith("import-")} == set(old["tags"].split())
        assert new["valid_from"] == old["valid_from"] and new["valid_to"] == old["valid_to"]
        assert md_notes.iso_day(new["created_at"]) == md_notes.iso_day(old["created_at"])
        assert new["expires_at"] is None and new["source_session"] == "import:hermind"
    assert [n["id"] for n in mine] == [1, 2, 3]                            # B's own ids


# -- M2 check 2: the same folder into the database it came from ---------------------------------------------------------------

def test_the_same_folder_into_its_own_database_is_all_same(tmp_path):
    db, out = tmp_path / "a.db", tmp_path / "out"
    ids = three_notes(db)
    export(db, out)
    before = dump(db)
    prev = import_core.run_import(str(out), db)
    assert prev["counts"]["duplicate"] == 3 and prev["counts"]["new"] == 0
    assert sorted(e["reason"] for e in prev["entries"]) == [f"same as #{i}" for i in sorted(ids)]
    res = import_core.run_import(str(out), db, apply=True)
    assert res["created"] == 0 and res["batch"] is None and res["backup"] is None
    assert dump(db) == before


def test_an_edited_copy_is_reported_and_never_applied(tmp_path):
    db, out = tmp_path / "a.db", tmp_path / "out"
    a, b, c = three_notes(db)
    export(db, out)
    f = next(out.rglob(f"{a:05d}-*.md"))
    f.write_text(f.read_text(encoding="utf-8").replace("keep 7 days", "keep 30 days"), encoding="utf-8")
    before = dump(db)
    res = import_core.run_import(str(out), db, apply=True)
    edited = next(e for e in res["entries"] if e["note_id"] == a and e["outcome"] != "duplicate")
    assert edited["outcome"] == "edited" and edited["reason"].startswith(f"edited copy of #{a}")
    assert "tools/import_md.py" in edited["reason"] and "differs in: content" in edited["reason"]
    assert res["created"] == 0 and dump(db) == before
    assert "edited copy of #" in hermind_cli.import_text(res)


def test_an_older_version_of_a_note_still_belongs_to_it(tmp_path):
    db, out = tmp_path / "a.db", tmp_path / "out"
    a, b, c = three_notes(db)
    export(db, out)
    s = DecisionStore(str(db))
    s.update(a, "Nimbus backups run daily at 04:00 and keep 7 days.")       # the database moves on after the export
    s.close()
    res = import_core.run_import(str(out), db, apply=True)
    assert res["created"] == 0
    old = next(e for e in res["entries"] if e["note_id"] == a)
    assert old["outcome"] == "edited" and "an older version of the note" in old["reason"]


# -- M2 check 3: someone else's ids -----------------------------------------------------------------------------------------

def other_numbering_db(path):
    s = DecisionStore(str(path))
    s.add("The staging bucket is named blue-heron.", "tech")
    s.update(1, "The staging bucket is named grey-heron.")
    s.close()


def test_a_file_with_a_foreign_id_and_no_rev_is_a_new_note(tmp_path, import_md):
    db, folder = tmp_path / "x.db", tmp_path / "files"
    other_numbering_db(db)
    folder.mkdir()
    (folder / "n.md").write_text("---\nid: 1\n---\nA completely different note about the lighthouse rota.\n", encoding="utf-8")
    before_note = [r for r in rows(db) if r["id"] == 1]
    hist_before = dump(db)["note_links"]
    prev = import_core.run_import(str(folder), db)
    assert prev["counts"]["new"] == 1 and prev["entries"][0]["outcome"] == "new"
    res = import_core.run_import(str(folder), db, apply=True, backup=False)
    assert res["created"] == 1
    after = rows(db)
    assert [r for r in after if r["id"] == 1] == before_note and len(after) == 2
    assert dump(db)["note_links"] == hist_before
    # tools/import_md.py: the same file against a fresh copy of the database
    fresh = tmp_path / "y.db"
    other_numbering_db(fresh)
    before_note = [r for r in rows(fresh) if r["id"] == 1]
    code, text = tool_import(import_md, fresh, folder, "--apply")
    assert code == 0 and "1 created" in text
    after = rows(fresh)
    assert [r for r in after if r["id"] == 1] == before_note and len(after) == 2
    assert any("lighthouse" in r["content"] for r in after)


def test_an_id_the_database_does_not_have_or_a_rev_that_never_belonged_is_a_new_note(tmp_path, import_md):
    db, folder = tmp_path / "x.db", tmp_path / "files"
    other_numbering_db(db)
    folder.mkdir()
    (folder / "ghost.md").write_text("---\nid: 99999\nrev: abcdef012345\n---\nNobody home with this id.\n", encoding="utf-8")
    (folder / "wrongrev.md").write_text("---\nid: 1\nrev: abcdef012345\n---\nAnother note, borrowed id one.\n", encoding="utf-8")
    res = import_core.run_import(str(folder), db)
    assert res["counts"]["new"] == 2
    code, text = tool_import(import_md, db, folder)
    assert "2 new" in text and "unknown id" not in text
    code, text = tool_import(import_md, db, folder, "--apply")
    assert code == 0 and "2 created" in text and len(rows(db)) == 3


# -- M2 check 4: import_md dry run = apply ------------------------------------------------------------------------------------

def test_import_md_dry_run_reports_what_apply_then_does(tmp_path, import_md):
    base = tmp_path / "base.db"
    s = DecisionStore(str(base))
    s.add("The office printer lives on the second floor next to the kitchen.", "state")
    s.forget(s.add("The old parking gate code was changed after the audit.", "state")[0])
    s.close()
    folder = tmp_path / "mixed"
    folder.mkdir()
    texts = ["Maria prefers vegetarian lunches on team days.", "  MARIA prefers vegetarian lunches on team days.  ", SECRET,
             ("lorem ipsum dolor " * 6000)[:100_500], "The old parking gate code was changed after the audit.", "",
             "The Aurora project ships its first beta on 2027-02-01.",
             "The office printer lives on the second floor next to the kitchen.",
             "<private>my sister's phone number is 555-0100</private>"]
    for i, t in enumerate(texts, 1):
        (folder / f"f{i}.md").write_text(t + "\n", encoding="utf-8")
    dry_db, apply_db = tmp_path / "dry.db", tmp_path / "apply.db"
    shutil.copy(base, dry_db)
    shutil.copy(base, apply_db)
    before = dump(dry_db)
    _, dry = tool_import(import_md, dry_db, folder)
    assert dump(dry_db) == before
    _, applied = tool_import(import_md, apply_db, folder, "--apply")
    parse = lambda t: dict((k.replace("to ", "").replace("created", "new").replace("updated", "update"), int(n)) for n, k in
                           re.findall(r"(\d+) ([a-z ]+?)(?:,|$|\n)", t.split("summary:")[1]))
    d, a = parse(dry), parse(applied)
    assert d == a, (dry, applied)
    assert d["new"] == 2 and d["skipped"] == 3 and d["refused"] == 4
    assert len(rows(apply_db)) == 3
    # and `hermind import` on the same folder makes the same decisions
    res = import_core.run_import(str(folder), base)
    assert res["counts"] == {"new": 2, "version": 0, "duplicate": 2, "edited": 0, "history": 0, "refused": 4, "unreadable": 0, "empty": 1, "excluded": 0}


# -- M2 check 5: edit-sync still works ---------------------------------------------------------------------------------------------

def test_edit_sync_still_updates_the_note_with_history(tmp_path, import_md):
    db, out = tmp_path / "a.db", tmp_path / "out"
    a, b, c = three_notes(db)
    export(db, out)
    f = next(out.rglob(f"{a:05d}-*.md"))
    f.write_text(f.read_text(encoding="utf-8").replace("keep 7 days", "keep 14 days"), encoding="utf-8")
    code, text = tool_import(import_md, db, out)
    assert code == 0 and "1 to update" in text and "0 new" in text
    code, text = tool_import(import_md, db, out, "--apply")
    assert code == 0 and "1 updated" in text
    s = DecisionStore(str(db))
    assert "keep 14 days" in s.get(a)["content"]
    old = s.conn.execute("SELECT content FROM notes WHERE status='superseded'").fetchall()
    assert len(old) == 1 and "keep 7 days" in old[0][0]
    s.close()
    assert tool_import(import_md, db, out, "--apply")[1].count("unchanged") == 1                         # now a no-op


def test_edit_sync_refuses_an_edit_that_would_store_a_secret_in_the_dry_run_too(tmp_path, import_md):
    db, out = tmp_path / "a.db", tmp_path / "out"
    a, b, c = three_notes(db)
    export(db, out)
    f = next(out.rglob(f"{a:05d}-*.md"))
    f.write_text(f.read_text(encoding="utf-8").replace("keep 7 days", SECRET), encoding="utf-8")
    code, dry = tool_import(import_md, db, out)
    code2, applied = tool_import(import_md, db, out, "--apply")
    assert "1 refused" in dry and "1 refused" in applied and code == code2 == 1


# -- the reader itself ---------------------------------------------------------------------------------------------------------------

def test_front_matter_is_honoured_and_unknown_keys_are_ignored(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("---\nkind: decision\nkeywords: [alpha, beta]\ntags: [Ops, infra]\nvalid_from: 2026-01-01\n"
                                 "valid_to: 2027-01-01\ncreated: 2026-03-04\nmood: sunny\n---\nWe chose the Falcon vendor.\n",
                                 encoding="utf-8")
    (folder / "b.md").write_text("---\nstatus: pending\n---\nProbably the Heron team owns the pager.\n", encoding="utf-8")
    res = import_core.run_import(str(folder), db, apply=True, backup=False)
    assert res["created"] == 2
    by = {r["content"].split()[0]: r for r in rows_all(db)}
    a = by["We"]
    assert a["kind"] == "decision" and a["keywords"] == "alpha beta" and set(a["tags"].split()) >= {"ops", "infra"}
    assert md_notes.iso_day(a["valid_from"]) == "2026-01-01" and md_notes.iso_day(a["valid_to"]) == "2027-01-01"
    assert md_notes.iso_day(a["created_at"]) == "2026-03-04" and a["review"] == "active" and a["expires_at"] is None
    p = by["Probably"]
    assert p["review"] == "pending" and p["status"] == "active" and p["expires_at"] is None
    s = DecisionStore(str(db))
    assert s.pending_count() == 1
    s.close()


def rows_all(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes ORDER BY id")]
    finally:
        s.close()


def test_unusable_front_matter_values_are_dropped_with_a_remark(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("---\nkind: idea\ncreated: whenever\n---\nBuy a second monitor for the lab.\n", encoding="utf-8")
    res = import_core.run_import(str(folder), db)
    assert res["counts"]["new"] == 1
    (entry,) = res["entries"]                       # M2: the entry's reason names each dropped field (no remark any more)
    assert "kind dropped" in entry["reason"] and "created dropped" in entry["reason"]


def test_single_files_hidden_folders_and_unreadable_files(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "notes"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "x.md").write_text("ignored\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "keep.md").write_text("The ferry leaves at nine.\n", encoding="utf-8")
    (folder / "bad.md").write_bytes(b"\xff\xfd\x80 not utf8")
    (folder / "list.txt").write_text("a text file in a folder\n", encoding="utf-8")
    (folder / "index.md").write_text("# index\n", encoding="utf-8")
    res = import_core.run_import(str(folder), db)
    # M3: .txt files and a hand-written index.md are notes; a file that cannot be decoded is an "unreadable" entry
    assert [e["origin"] for e in res["entries"]] == ["bad.md", "index.md", "list.txt", "sub/keep.md"]
    assert res["counts"]["unreadable"] == 1 and res["counts"]["new"] == 3
    assert "--encoding" in next(e["reason"] for e in res["entries"] if e["origin"] == "bad.md")
    assert "x.md" not in " ".join(res["remarks"]) and ".git/" in " ".join(res["remarks"])
    one = tmp_path / "one.txt"
    one.write_text("The ferry leaves at nine thirty.\n", encoding="utf-8")
    res = import_core.run_import(str(one), db)
    assert res["reader"] == "markdown" and res["entries"][0]["origin"] == "one.txt" and res["counts"]["new"] == 1


def test_exclude_matches_relative_paths_and_revert_works_for_markdown(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "notes"
    (folder / "old").mkdir(parents=True)
    (folder / "old" / "a.md").write_text("Old fact about the cellar key.\n", encoding="utf-8")
    (folder / "b.md").write_text("Fresh fact about the loft ladder.\n", encoding="utf-8")
    res = import_core.run_import(str(folder), db, apply=True, backup=False, exclude=["old/*"])
    assert res["counts"]["excluded"] == 1 and res["created"] == 1
    out = import_core.run_revert(res["batch"], db, apply=True, backup=False)
    assert len(out["remove"]) == 1 and rows_all(db) == []


def test_cli_end_to_end_in_a_subprocess(tmp_path):
    import subprocess
    a_db, out = tmp_path / "a.db", tmp_path / "out"
    three_notes(a_db)
    export(a_db, out)
    b_db = tmp_path / "b.db"
    run = lambda *args: subprocess.run([sys.executable, str(ROOT / "hermind_cli.py"), "import", *args], capture_output=True,
                                       text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=120)
    r = run(str(out), "--db", str(b_db))
    assert r.returncode == 0 and "PREVIEW" in r.stdout and "3 new" in r.stdout and not b_db.exists()
    r = run(str(out), "--db", str(b_db), "--apply", "--json")
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["created"] == 3 and d["reader"] == "markdown"
    r = run("--list", "--db", str(b_db))
    assert d["batch"] in r.stdout
    r = run("--readers")
    assert r.returncode == 0 and "markdown" in r.stdout



def test_edit_back_to_forgotten_text_is_refused_not_a_traceback(import_md, tmp_path):
    db, out = tmp_path / "t.db", tmp_path / "out"
    a, b, _ = three_notes(db)
    s = DecisionStore(str(db))
    gone = "Zorblax backup runs at 03:00 every night on node seven."
    s.forget(s.add(gone, "tech")[0])
    s.close()
    export(db, out)
    f = next(p for p in out.rglob("*.md") if f"id: {a}\n" in p.read_text(encoding="utf-8"))
    text = f.read_text(encoding="utf-8")
    body = "Nimbus backups run daily at 03:00 and keep 7 days."
    f.write_text(text.replace(body, gone), encoding="utf-8")
    for flags in ((), ("--apply",)):
        code, report = tool_import(import_md, db, out, *flags)
        assert code == 1 and "refused" in report and "forgotten" in report and "Traceback" not in report
    assert rows(db)[0]["content"] == body


# -- symbolic links are never followed ---------------------------------------------------------------------------------

import os  # noqa: E402

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")


def _tree_with_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host.md").write_text("Zorblax backup runs at 03:00 on node seven.\n", encoding="utf-8")
    (outside / "deep.md").write_text("Quillon reports are due on Fridays.\n", encoding="utf-8")
    (outside / ".hid").mkdir()
    (outside / ".hid" / "x.md").write_text("Plinth rota changes on Monday.\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "own.md").write_text("The Nimbus cache keeps entries for ten minutes.\n", encoding="utf-8")
    os.symlink(outside / "host.md", src / "escape.md")             # a file link
    os.symlink(outside, src / "linked-dir")                        # a folder link
    os.symlink(outside / ".hid", src / ".hidden-link")             # a link to a folder, with a hidden name
    os.symlink(src / "missing.md", src / "broken.md")
    return src


@posix_only
def test_walk_folder_lists_links_and_never_follows_them(tmp_path):
    src = _tree_with_links(tmp_path)
    w = md_notes.walk_folder(src)
    assert [p.name for p in w.notes] == ["own.md"]
    assert sorted(p.name for p in w.links) == [".hidden-link", "broken.md", "escape.md", "linked-dir"]


@posix_only
def test_hermind_import_skips_symlinks_and_reports_them(tmp_path):
    src = _tree_with_links(tmp_path)
    db = tmp_path / "t.db"
    res = import_core.run_import(str(src), db, apply=True, backup=False)
    assert res["counts"]["new"] == 1 and res["counts"]["unreadable"] == 4
    by = {e["origin"]: e for e in res["entries"] if e["outcome"] == "unreadable"}
    assert sorted(by) == [".hidden-link/", "broken.md", "escape.md", "linked-dir/"]
    assert all("symbolic link" in e["reason"] for e in by.values())
    s = DecisionStore(str(db))
    try:
        texts = [r[0] for r in s.conn.execute("SELECT content FROM notes")]
        assert len(texts) == 1 and "Nimbus" in texts[0]
    finally:
        s.close()


@posix_only
def test_single_file_given_directly_is_followed_even_when_it_is_a_link(tmp_path):
    # the user named this file: following it is a decision, not an escape
    outside = tmp_path / "outside.md"
    outside.write_text("Zorblax backup runs at 03:00 on node seven.\n", encoding="utf-8")
    link = tmp_path / "named.md"
    os.symlink(outside, link)
    res = import_core.run_import(str(link), tmp_path / "t.db", backup=False)
    assert res["counts"]["new"] == 1


@posix_only
def test_import_md_tool_skips_symlinks_and_reports_them(import_md, tmp_path):
    src = _tree_with_links(tmp_path)
    db = tmp_path / "t.db"
    DecisionStore(str(db)).close()
    code, report = tool_import(import_md, db, src, "--apply")
    assert code == 0 and report.count("symbolic link") == 4 and "4 skipped" in report and "1 created" in report
    assert all("Zorblax" not in r["content"] and "Quillon" not in r["content"] for r in rows(db))
