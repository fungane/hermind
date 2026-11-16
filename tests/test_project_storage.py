"""notes.project: the column, the schema upgrade, which writes store it, and that it never leaves the database."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import export_notes
import md_export
from project import normalize
from store import DecisionStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_fixture import build_db  # noqa: E402

ATLAS = "/srv/work/atlas"
BOREALIS = "/srv/work/borealis"


def _projects(store):
    return {r[0]: r[1] for r in store.conn.execute("SELECT id, project FROM notes")}


def _v5_db(path: Path, rows: int = 3) -> list[int]:
    """A database as schema 5 wrote it: no project column, user_version 5."""
    s = DecisionStore(str(path))
    ids = [s.add(f"Atlas decision number {i}: orders use PostgreSQL {i}", kind="decision")[0] for i in range(rows)]
    s.conn.execute("DROP INDEX notes_project")
    s.conn.execute("ALTER TABLE notes DROP COLUMN project")
    s.conn.execute("PRAGMA user_version=5")
    s.conn.commit()
    s.conn.close()
    return ids


needs_drop_column = pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="ALTER TABLE DROP COLUMN needs SQLite 3.35")


# -- schema ---------------------------------------------------------------------------------------------------

@needs_drop_column
def test_a_v5_database_is_backed_up_then_upgraded_and_keeps_every_row(tmp_path):
    db = tmp_path / "m.db"
    ids = _v5_db(db)
    s = DecisionStore(str(db))
    try:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "project" in {r[1] for r in s.conn.execute("PRAGMA table_info(notes)")}
        assert [s.get(i)["content"] for i in ids] == [f"Atlas decision number {n}: orders use PostgreSQL {n}" for n in range(3)]
        assert set(_projects(s).values()) == {None}
    finally:
        s.close()
    baks = list(tmp_path.glob("m.db.bak-v5-*"))
    assert len(baks) == 1
    old = sqlite3.connect(str(baks[0]))
    assert old.execute("PRAGMA user_version").fetchone()[0] == 5
    assert old.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
    assert "project" not in {r[1] for r in old.execute("PRAGMA table_info(notes)")}


@needs_drop_column
def test_the_upgrade_is_refused_when_its_backup_fails(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _v5_db(db)
    import db_backup
    monkeypatch.setattr(db_backup, "_copy", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="could not back it up"):
        DecisionStore(str(db))
    con = sqlite3.connect(str(db))
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5
    assert "project" not in {r[1] for r in con.execute("PRAGMA table_info(notes)")}


def test_a_current_database_gets_no_backup_and_the_column_is_nullable(tmp_path):
    DecisionStore(str(tmp_path / "a.db")).close()
    DecisionStore(str(tmp_path / "a.db")).close()
    assert not list(tmp_path.glob("*.bak-*"))
    con = sqlite3.connect(str(tmp_path / "a.db"))
    col = next(r for r in con.execute("PRAGMA table_info(notes)") if r[1] == "project")
    assert col[2].upper() == "TEXT" and col[3] == 0 and col[4] is None      # type, notnull, default


def test_a_v6_database_works_for_code_that_does_not_know_the_column(tmp_path):
    """The insert the schema-5 code ran (it names no project column) still succeeds, and the row is found."""
    s = DecisionStore(str(tmp_path / "a.db"))
    s.add("Atlas keeps orders in PostgreSQL", kind="decision", project=ATLAS)
    old_insert = ("INSERT INTO notes(content, kind, keywords, created_at, expires_at, source_session, pinned, content_hash, "
                  "doc_len_content, doc_len_keywords, event_at, proof_count, candidate, scope, tags, review, valid_from, "
                  "valid_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    with s.conn:
        cur = s.conn.execute(old_insert, ("Borealis queues use Kafka", "decision", "", 1.0, None, "", 0, "h", 4, 0, None, 1, 0,
                                          "default", "", "active", None, None))
        s._index(cur.lastrowid, ["borealis", "kafka"], [])
    assert _projects(s)[cur.lastrowid] is None
    assert [h["id"] for h in s.search("Borealis Kafka")] == [cur.lastrowid]
    assert [h["id"] for h in s.search("Atlas PostgreSQL")]
    s.close()


# -- store.add / update ---------------------------------------------------------------------------------------

def test_add_stores_the_project_of_a_new_note_only(store):
    a, created = store.add("Atlas keeps orders in PostgreSQL", kind="decision", project=ATLAS)
    assert created and _projects(store)[a] == ATLAS
    again, created = store.add("Atlas keeps orders in PostgreSQL", kind="decision", project=BOREALIS)
    assert (again, created) == (a, False) and _projects(store)[a] == ATLAS
    near, created = store.add("Atlas keeps orders in PostgreSQL 16", kind="decision", project=BOREALIS, dedup_threshold=0.5)
    assert (near, created) == (a, False) and _projects(store)[a] == ATLAS
    n, _ = store.add("No project here", kind="chat")
    e, _ = store.add("Empty string means unknown", kind="chat", project="")
    assert _projects(store)[n] is None and _projects(store)[e] is None


def test_update_keeps_the_project_and_the_history_row_copies_it(store):
    nid, _ = store.add("Atlas keeps orders in PostgreSQL 15", kind="decision", project=ATLAS)
    assert store.update(nid, "Atlas keeps orders in PostgreSQL 16")
    rows = store.conn.execute("SELECT status, project FROM notes").fetchall()
    assert sorted((r[0], r[1]) for r in rows) == [("active", ATLAS), ("superseded", ATLAS)]


def test_an_older_version_added_by_an_import_has_no_project(store):
    nid, _ = store.add("Atlas keeps orders in PostgreSQL 16", kind="decision", project=ATLAS)
    hist = store.add_older_version(nid, "Atlas keeps orders in PostgreSQL 15", kind="decision", created_at=1.0)
    assert _projects(store)[hist] is None


def test_known_projects_lists_each_distinct_value_once(store):
    store.add("one", project=ATLAS)
    store.add("two", project=BOREALIS)
    store.add("three", project=ATLAS)
    store.add("four")
    assert store.known_projects() == [ATLAS, BOREALIS]


# -- project stays inside the database ------------------------------------------------------------------------

def test_a_note_dict_has_no_project_key(store):
    nid, _ = store.add("Atlas keeps orders in PostgreSQL", kind="decision", project=ATLAS)
    store.update(nid, "Atlas keeps orders in PostgreSQL 16")
    seen = [store.get(nid), *store.list(), *store.list(include_hidden=True), *store.search("Atlas PostgreSQL"),
            *store.history(nid), *store.related("Atlas")["notes"], *store.notes_by_ids([nid])]
    assert seen
    for note in seen:
        assert "project" not in note, note


def test_exports_have_no_project(tmp_path):
    db = tmp_path / "a.db"
    build_db(db)
    s = DecisionStore(str(db))
    try:
        s.add("Atlas keeps orders in PostgreSQL", kind="decision", project=ATLAS)
        assert any(project for (project,) in s.conn.execute("SELECT project FROM notes"))
        for rec in export_notes.collect_records(s.conn):
            assert "project" not in rec
        for n in md_export.collect(s, True, "*"):
            assert "project" not in n
        out = tmp_path / "out.json"
        export_notes.run_export(s, fmt="json", out=str(out), force=True)
        jl = tmp_path / "out.jsonl"
        export_notes.run_export(s, fmt="jsonl", out=str(jl), force=True)
        md = tmp_path / "md"
        md_export.write_folder(s, md, include_history=True)
    finally:
        s.close()
    assert ATLAS not in out.read_text() and ATLAS not in jl.read_text()
    assert all(ATLAS not in p.read_text() for p in md.rglob("*.md"))
    assert '"project"' not in out.read_text()


def test_an_import_leaves_the_project_empty(tmp_path):
    import import_core
    src = tmp_path / "a.db"
    build_db(src)
    s = DecisionStore(str(src))
    s.add("Atlas keeps orders in PostgreSQL", kind="decision", project=ATLAS)
    out = tmp_path / "a.json"
    export_notes.run_export(s, fmt="json", out=str(out), force=True)
    s.close()
    dst = tmp_path / "b.db"
    import_core.run_import(str(out), dst, apply=True, backup=False)
    b = DecisionStore(str(dst))
    try:
        assert b.count() > 0
        assert set(_projects(b).values()) == {None}
    finally:
        b.close()


# -- provider -------------------------------------------------------------------------------------------------

def _provider(pkg, tmp_path, cwd="unset", **cfg):
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db", "consolidate_on_init": False, **cfg})
    kw = {} if cwd == "unset" else {"cwd": cwd}
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram", **kw)
    return p


def _db_projects(p):
    return {r[0]: r[1] for r in p._store.conn.execute("SELECT content, project FROM notes WHERE status='active'")}


class FakeLLM:
    capture_extract = True
    last_usage = {}

    def __init__(self, facts):
        self.facts = facts

    def extract_facts(self, user, assistant):
        return self.facts

    def extract_entities(self, content):
        return []


def test_the_provider_fixes_its_project_from_the_cwd_and_keeps_it(pkg, tmp_path):
    p = _provider(pkg, tmp_path, cwd=ATLAS + "/")
    try:
        assert p._project == normalize(ATLAS)
        p.on_session_switch("20261018_130000_efgh", reset=True)
        assert p._project == normalize(ATLAS)
    finally:
        p.shutdown()


def test_no_cwd_home_or_root_means_no_project(pkg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for cwd in ("unset", None, "", "/", str(home), str(home) + "/"):
        p = _provider(pkg, tmp_path, cwd=cwd)
        try:
            assert p._project is None, cwd
            nid = json.loads(p.handle_tool_call("memory_remember", {"content": f"note for {cwd!r}", "keywords": "x"}))["id"]
            assert p._store.conn.execute("SELECT project FROM notes WHERE id=?", (nid,)).fetchone()[0] is None
        finally:
            p.shutdown()


def test_an_umbrella_folder_is_neutral_but_one_project_below_it_is_not(pkg, tmp_path):
    s = DecisionStore(str(tmp_path / "dm.db"))
    s.add("a", project="/srv/umb/one")
    s.add("b", project="/srv/umb/two")
    s.close()
    p = _provider(pkg, tmp_path, cwd="/srv/umb")
    assert p._project is None
    p.shutdown()
    p = _provider(pkg, tmp_path, cwd="/srv/umb/one")
    assert p._project == normalize("/srv/umb/one")
    p.shutdown()
    p = _provider(pkg, tmp_path, cwd="/srv/umbrella")           # a longer name, not a folder above them
    assert p._project == normalize("/srv/umbrella")
    p.shutdown()


def test_a_database_problem_while_finding_the_project_means_no_project(pkg, tmp_path, monkeypatch):
    monkeypatch.setattr(pkg.DecisionStore, "known_projects", lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    p = _provider(pkg, tmp_path, cwd=ATLAS)
    try:
        assert p._project is None
    finally:
        p.shutdown()


def test_memory_remember_stores_the_project(pkg, tmp_path):
    p = _provider(pkg, tmp_path, cwd=ATLAS)
    try:
        r = json.loads(p.handle_tool_call("memory_remember", {"content": "Atlas keeps orders in PostgreSQL", "keywords": "db"}))
        assert p._store.conn.execute("SELECT project FROM notes WHERE id=?", (r["id"],)).fetchone()[0] == normalize(ATLAS)
        out = p.handle_tool_call("memory_get", {"id": r["id"]}) + p.handle_tool_call("memory_search", {"query": "Atlas PostgreSQL"})
        assert "project" not in out and ATLAS not in out
    finally:
        p.shutdown()


def test_auto_capture_stores_the_project(pkg, tmp_path):
    p = _provider(pkg, tmp_path, cwd=ATLAS)
    try:
        p.sync_turn("How do we deploy the Atlas billing service?", "Use the blue green script from the runbook.")
        rows = _db_projects(p)
        assert len(rows) == 1 and set(rows.values()) == {normalize(ATLAS)}
    finally:
        p.shutdown()


def test_llm_extraction_stores_the_project(pkg, tmp_path):
    p = _provider(pkg, tmp_path, cwd=ATLAS, review_queue=False)
    try:
        p._llm = FakeLLM([{"text": "Atlas billing is deployed with the blue green script", "aliases": ["deploy"], "kind": "tech"}])
        p._extract_and_store("How do we deploy Atlas billing?", "Use the blue green script.", "chunk", "sid")
        assert _db_projects(p) == {"Atlas billing is deployed with the blue green script": normalize(ATLAS)}
        p._llm = FakeLLM(None)                          # model failure: the raw turn is stored instead, also with the project
        p._extract_and_store("Where do logs go?", "To the central bucket.", "User: Where do logs go?\nAssistant: To the central bucket.", "sid")
        assert set(_db_projects(p).values()) == {normalize(ATLAS)} and len(_db_projects(p)) == 2
    finally:
        p.shutdown()


def test_pre_compress_and_the_builtin_mirror_store_the_project(pkg, tmp_path):
    p = _provider(pkg, tmp_path, cwd=ATLAS)
    try:
        msgs = [{"role": "user", "content": "We decided to use blue green deployments for the Atlas billing service."},
                {"role": "assistant", "content": "Noted: the Atlas billing service will use blue green deployments."}]
        p.on_pre_compress(msgs)
        p.on_memory_write("add", "user", "Prefers short answers")
        rows = _db_projects(p)
        assert len(rows) >= 2 and set(rows.values()) == {normalize(ATLAS)}
    finally:
        p.shutdown()


def test_refreshing_or_merging_an_existing_note_keeps_its_project(pkg, tmp_path):
    a = _provider(pkg, tmp_path, cwd=ATLAS)
    nid = json.loads(a.handle_tool_call("memory_remember", {"content": "Atlas billing is deployed with the blue green script",
                                                             "keywords": "deploy"}))["id"]
    a.shutdown()
    b = _provider(pkg, tmp_path, cwd=BOREALIS, review_queue=False)
    try:
        r = json.loads(b.handle_tool_call("memory_remember", {"content": "Atlas billing is deployed with the blue green script",
                                                              "keywords": "release"}))
        assert r["id"] == nid and not r["created"]
        assert b._merge_into_twin("Atlas billing is deployed with the blue green script and a canary step", "canary")
        proj = {r[0]: r[1] for r in b._store.conn.execute("SELECT id, project FROM notes")}
        assert proj[nid] == normalize(ATLAS)
        assert set(proj.values()) == {normalize(ATLAS)}      # the history row of the merge carries it too
        assert len(proj) == 2
    finally:
        b.shutdown()


def test_the_mcp_provider_has_no_project(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": str(tmp_path / "x.db")})
    assert p._project is None
