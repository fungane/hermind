"""hermind import-projects: give notes written before the upgrade the project of their session, from a file the user makes."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from store import DecisionStore

ROOT = os.path.abspath(os.sep)                      # "/" on POSIX, a drive root on Windows
WORK = os.path.normcase(os.path.join(ROOT, "srv", "work"))


def w(*parts):
    """An absolute, normalised path under the invented work folder: absolute on every OS, so import-projects accepts it."""
    return os.path.normcase(os.path.join(WORK, *parts))


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "hermind" / "hermind_cli.py"
needs_drop_column = pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="ALTER TABLE DROP COLUMN needs SQLite 3.35")


def database_used(r):
    """The database path the command reported, from --json or the first text line; None when it reported none."""
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return str(data["database"]) if data.get("database") else None
    first = (r.stdout or "").splitlines()[0] if r.stdout else ""
    return first.split(" opened ", 1)[0].removeprefix("dry run: ") if " opened " in first else None


def run(home, *args):
    # A Hermes session exports HERMES_HOME; without this the command would resolve and open the developer's real
    # database instead of the test's. HOME/USERPROFILE point at the same empty test home.
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(Path(home) / ".hermes")}
    env.pop("HERMIND_DB", None)
    r = subprocess.run([sys.executable, str(CLI), "import-projects", *args], capture_output=True, text=True, env=env)
    used = database_used(r)
    if used is not None:      # a test that silently opens a real database outside tmp_path must fail here
        assert Path(used).resolve().is_relative_to(Path(home).parent.resolve()), f"used {used}, outside the test area"
    return r


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def projects(db):
    con = sqlite3.connect(db)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT id, project FROM notes")}
    finally:
        con.close()


def write_map(tmp_path, mapping, name="map.json"):
    f = tmp_path / name
    f.write_text(json.dumps(mapping), encoding="utf-8")
    return f


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def build(tmp_path, notes):
    """notes: [(text, session, project, status)] -> (db path, ids). "review" is a note waiting in the review queue
    (status active, review pending) and "rejected" one rejected from it (status rejected)."""
    db = tmp_path / "m.db"
    s = DecisionStore(str(db))
    ids = []
    for i, (text, sess, proj, status) in enumerate(notes):
        review = "pending" if status in ("review", "rejected") else "active"
        nid, _ = s.add(f"{text} number {i}", kind="chat", source_session=sess, project=proj, review=review)
        if status == "rejected":
            assert s.reject(nid)
        elif status not in ("active", "review"):
            s.conn.execute("UPDATE notes SET status=? WHERE id=?", (status, nid))
        ids.append(nid)
    s.conn.commit()
    s.conn.close()
    return db, ids


def test_sets_only_null_projects_and_leaves_a_set_project_alone(tmp_path, home):
    db, ids = build(tmp_path, [("orders use PostgreSQL", "s1", None, "active"),
                               ("billing runs nightly", "s1", w("old"), "active"),
                               ("queue depth alert", "s2", None, "active")])
    f = write_map(tmp_path, {"s1": w("atlas"), "s2": w("borealis")})
    r = run(home, str(f), "--db", str(db), "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["examined"], out["set"], out["skipped_has_project"]) == (3, 2, 1)
    assert out["unmatched_sessions"] == 0 and out["neutral_sessions"] == 0 and out["projects_after"] == 3
    assert projects(db) == {ids[0]: w("atlas"), ids[1]: w("old"), ids[2]: w("borealis")}


def test_history_and_the_review_queue_are_covered_but_a_rejected_note_is_not(tmp_path, home):
    db, ids = build(tmp_path, [("a", "s1", None, "superseded"), ("b", "s1", None, "merged"), ("c", "s1", None, "active"),
                               ("d", "s1", None, "review"), ("e", "s1", None, "rejected")])
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db), "--json")
    assert r.returncode == 0
    got = projects(db)
    assert [got[i] for i in ids] == [w("atlas")] * 4 + [None]   # a review-queue note is still active
    assert json.loads(r.stdout)["set"] == 4


def test_home_root_and_an_umbrella_folder_stay_null(tmp_path, home):
    db, ids = build(tmp_path, [("n", "home", None, "active"), ("n", "root", None, "active"), ("n", "empty", None, "active"),
                               ("n", "umb", None, "active"), ("n", "a", None, "active"), ("n", "b", None, "active")])
    f = write_map(tmp_path, {"home": str(home), "root": ROOT, "empty": "  ", "umb": WORK,
                             "a": w("atlas"), "b": w("borealis", "api")})
    out = json.loads(run(home, str(f), "--db", str(db), "--json").stdout)
    got = projects(db)
    assert [got[i] for i in ids] == [None, None, None, None, w("atlas"), w("borealis", "api")]
    assert out["set"] == 2 and out["neutral_sessions"] == 4 and out["projects_after"] == 2


def test_an_umbrella_over_one_stored_and_one_new_project_is_neutral(tmp_path, home):
    db, ids = build(tmp_path, [("n", "x", w("atlas"), "active"), ("n", "umb", None, "active"), ("n", "b", None, "active")])
    run(home, str(write_map(tmp_path, {"umb": WORK, "b": w("borealis")})), "--db", str(db))
    assert projects(db)[ids[1]] is None and projects(db)[ids[2]] == w("borealis")


def test_a_session_not_in_the_file_is_untouched_and_unmatched_sessions_are_counted(tmp_path, home):
    db, ids = build(tmp_path, [("n", "s1", None, "active"), ("n", "other", None, "active"), ("n", None, None, "active")])
    out = json.loads(run(home, str(write_map(tmp_path, {"s1": w("atlas"), "ghost": w("x")})),
                         "--db", str(db), "--json").stdout)
    assert projects(db) == {ids[0]: w("atlas"), ids[1]: None, ids[2]: None}
    assert out["unmatched_sessions"] == 1 and out["examined"] == 1


def test_dry_run_leaves_the_file_byte_identical_and_makes_no_backup(tmp_path, home):
    db, ids = build(tmp_path, [("n", "s1", None, "active")])
    before, listing = digest(db), sorted(os.listdir(tmp_path))
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db), "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dry_run"] is True and out["set"] == 1 and out["backup"] is None and out["opened"].startswith("read-only")
    assert digest(db) == before
    assert sorted(os.listdir(tmp_path)) == sorted(listing + ["map.json"])
    assert not list(tmp_path.glob("m.db.bak-*"))
    assert projects(db)[ids[0]] is None


def test_a_real_run_makes_the_backup_first_and_it_holds_the_old_state(tmp_path, home):
    db, ids = build(tmp_path, [("n", "s1", None, "active")])
    out = json.loads(run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db), "--json").stdout)
    baks = list(tmp_path.glob("m.db.bak-projects-*"))
    assert len(baks) == 1 and out["backup"] == str(baks[0])
    assert projects(baks[0]) == {ids[0]: None}
    assert projects(db) == {ids[0]: w("atlas")}


def test_running_it_twice_changes_nothing_the_second_time(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active"), ("n", "s2", None, "active")])
    f = write_map(tmp_path, {"s1": w("atlas"), "s2": str(home)})
    assert run(home, str(f), "--db", str(db)).returncode == 0
    after_first = digest(db)
    r = run(home, str(f), "--db", str(db), "--json")
    out = json.loads(r.stdout)
    assert out["set"] == 0 and out["skipped_has_project"] == 1 and out["backup"] is None
    assert digest(db) == after_first
    assert len(list(tmp_path.glob("m.db.bak-projects-*"))) == 1


def test_only_the_project_column_changes(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active"), ("n", "s2", None, "active")])
    def dump():
        con = sqlite3.connect(db)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(notes)") if r[1] != "project"]
            return (con.execute(f"SELECT {','.join(cols)} FROM notes ORDER BY id").fetchall(),
                    con.execute("SELECT * FROM postings ORDER BY 1,2").fetchall())
        finally:
            con.close()
    before = dump()
    run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db))
    assert dump() == before


def test_the_summary_never_prints_a_path_unless_asked_in_json(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active")])
    f = write_map(tmp_path, {"s1": w("secretplace")})
    r = run(home, str(f), "--db", str(db), "--dry-run", "--show-projects")
    assert "secretplace" not in r.stdout + r.stderr
    plain = json.loads(run(home, str(f), "--db", str(db), "--dry-run", "--json").stdout)
    assert "project_list" not in plain and "secretplace" not in json.dumps(plain)
    shown = json.loads(run(home, str(f), "--db", str(db), "--dry-run", "--json", "--show-projects").stdout)
    assert shown["project_list"] == [w("secretplace")]


def test_the_text_summary_has_every_figure(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active")])
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas"), "ghost": os.path.join(ROOT, "x", "y")})), "--db", str(db))
    assert r.returncode == 0
    for want in ("notes examined: 1", "notes set: 1", "already had a project): 0", "matched no note: 1",
                 "neutral folder", "distinct projects after the run: 1", "backup:"):
        assert want in r.stdout


def test_a_tilde_value_is_expanded_to_the_provider_project(tmp_path, home):
    from project import normalize
    db, ids = build(tmp_path, [("n", "s1", None, "active")])
    r = run(home, str(write_map(tmp_path, {"s1": "~/atlas"})), "--db", str(db), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert projects(db) == {ids[0]: normalize(str(home / "atlas"))}


@pytest.mark.parametrize("mapping, message", [
    ({"s1": "work/atlas"}, "absolute"), ({"s1": "./atlas"}, "absolute"),
    ({"": w("atlas")}, "session id"), ({"   ": w("atlas")}, "session id")])
def test_a_relative_value_or_an_empty_session_id_is_exit_2(tmp_path, home, mapping, message):
    db, _ = build(tmp_path, [("n", "s1", None, "active"), ("n", "", None, "active")])
    before = digest(db)
    r = run(home, str(write_map(tmp_path, mapping)), "--db", str(db))
    assert r.returncode == 2 and message in r.stdout and "Traceback" not in r.stdout + r.stderr
    assert digest(db) == before and not list(tmp_path.glob("m.db.bak-*"))


def test_a_relative_value_names_the_session_ids_but_not_their_paths(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active"), ("n", "s2", None, "active")])
    r = run(home, str(write_map(tmp_path, {"s1": "work/secretplace", "s2": w("atlas")})), "--db", str(db))
    assert r.returncode == 2 and "'s1'" in r.stdout and "'s2'" not in r.stdout
    assert "secretplace" not in r.stdout and w("atlas") not in r.stdout


@pytest.mark.parametrize("content, message", [
    ("{not json", "not valid JSON"), ("[1, 2]", "must be a JSON object"), ('"text"', "must be a JSON object"),
    ('{"s1": 5}', "must be a string"), ('{"s1": null}', "must be a string"), ('{"s1": ["/a"]}', "must be a string")])
def test_a_malformed_file_is_exit_2_with_a_clear_message(tmp_path, home, content, message):
    db, _ = build(tmp_path, [("n", "s1", None, "active")])
    f = tmp_path / "bad.json"
    f.write_text(content)
    before = digest(db)
    r = run(home, str(f), "--db", str(db))
    assert r.returncode == 2 and message in r.stdout and "Traceback" not in r.stdout + r.stderr
    assert digest(db) == before and not list(tmp_path.glob("m.db.bak-*"))


def test_a_missing_file_or_database_is_exit_2(tmp_path, home):
    db, _ = build(tmp_path, [("n", "s1", None, "active")])
    assert run(home, str(tmp_path / "nope.json"), "--db", str(db)).returncode == 2
    f = write_map(tmp_path, {})
    r = run(home, str(f), "--db", str(tmp_path / "absent.db"))
    assert r.returncode == 2 and "does not exist" in r.stdout
    r = run(home, str(f))                                  # no --db, empty home: nothing to find
    assert r.returncode == 2 and "no database found" in r.stdout


def test_a_file_that_is_not_a_database_is_a_failure_not_a_traceback(tmp_path, home):
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    r = run(home, str(write_map(tmp_path, {"s": os.path.join(ROOT, "a", "b")})), "--db", str(bad))
    assert r.returncode == 1 and r.stdout.startswith("ERROR") and "Traceback" not in r.stderr


def test_profile_picks_the_database_of_that_hermes_profile(tmp_path, home):
    pdir = home / ".hermes" / "profiles" / "work"
    pdir.mkdir(parents=True)
    db = pdir / "hermind.db"
    s = DecisionStore(str(db))
    nid, _ = s.add("orders use PostgreSQL", kind="chat", source_session="s1")
    s.conn.commit()
    s.conn.close()
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--profile", "work", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert projects(db) == {nid: w("atlas")}


# -- a database from before the project column ----------------------------------------------------------------

def v5_db(tmp_path):
    db, ids = build(tmp_path, [("n", "s1", None, "active"), ("n", "s2", None, "active")])
    con = sqlite3.connect(db)
    con.execute("DROP INDEX notes_project")
    con.execute("ALTER TABLE notes DROP COLUMN project")
    con.execute("PRAGMA user_version=5")
    con.commit()
    con.close()
    return db, ids


@needs_drop_column
def test_dry_run_does_not_upgrade_a_v5_database_on_disk(tmp_path, home):
    db, ids = v5_db(tmp_path)
    before = digest(db)
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db), "--dry-run", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["set"] == 1 and out["opened"].startswith("read-only") and "schema 5" in out["opened"]
    assert digest(db) == before
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5
    assert "project" not in [c[1] for c in con.execute("PRAGMA table_info(notes)")]
    con.close()
    assert not list(tmp_path.glob("m.db.bak-*"))


@needs_drop_column
def test_a_real_run_on_a_v5_database_backs_up_then_upgrades_then_sets(tmp_path, home):
    db, ids = v5_db(tmp_path)
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})), "--db", str(db), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert projects(db) == {ids[0]: w("atlas"), ids[1]: None}
    assert len(list(tmp_path.glob("m.db.bak-projects-*"))) == 1 and len(list(tmp_path.glob("m.db.bak-v5-*"))) == 1
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 6
    con.close()


@needs_drop_column
def test_a_real_run_with_nothing_to_set_does_not_upgrade_a_v5_database(tmp_path, home):
    db, _ = v5_db(tmp_path)
    before = digest(db)
    r = run(home, str(write_map(tmp_path, {"unknown": w("atlas")})), "--db", str(db))
    assert r.returncode == 0 and digest(db) == before and not list(tmp_path.glob("m.db.bak-*"))


def test_help_lists_the_command():
    r = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True)
    assert "import-projects" in r.stdout
    assert "import-projects" in (CLI.read_text(encoding="utf-8").split('"""')[1])


def test_no_database_outside_the_test_home_is_ever_found(tmp_path, home, tmp_path_factory, monkeypatch):
    external = tmp_path_factory.mktemp("external-hermes")
    db = external / "hermind.db"
    s = DecisionStore(str(db))
    s.add("orders use PostgreSQL", kind="chat", source_session="s1")
    s.conn.commit()
    s.conn.close()
    before = digest(db)
    monkeypatch.setenv("HERMES_HOME", str(external))       # a developer's real Hermes home, inherited by os.environ
    r = run(home, str(write_map(tmp_path, {"s1": w("atlas")})))
    assert r.returncode == 2 and "no database found" in r.stdout
    assert digest(db) == before and not list(external.glob("hermind.db-*"))
