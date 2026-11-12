"""SPEC-4 M1: `hermind export` hands over every note in every status, with every field, the links and the entities."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_fixture import build_db, table_dump  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def cli(tmp_path, *args, env=None):
    e = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), **(env or {})}
    for k in ("HERMIND_DB", "HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE"):
        if not env or k not in env:
            e.pop(k, None)
    return subprocess.run([sys.executable, str(ROOT / "hermind_cli.py"), *args], capture_output=True, text=True, env=e)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "a.db"
    build_db(path)
    return path


def export_json(tmp_path, db, *flags):
    r = cli(tmp_path, "export", "--db", str(db), *flags)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout)


def raw_rows(db, sql, args=()):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def test_the_fixture_has_every_kind_of_row(db):
    rows = {r["id"]: r for r in raw_rows(db, "SELECT * FROM notes")}
    assert {r["status"] for r in rows.values()} >= {"active", "merged", "superseded", "rejected"}
    assert any(r["review"] == "pending" and r["status"] == "active" for r in rows.values())
    assert any(r["pinned"] for r in rows.values())
    assert any(r["trust"] == 0.9 and r["tags"] and r["valid_from"] and r["valid_to"] and r["scope"] != "default"
               for r in rows.values())
    rels = {r["rel"] for r in raw_rows(db, "SELECT rel FROM note_links")}
    assert rels >= {"version_of", "merged_into", "superseded_by", "stale_by"}
    assert any(len([a for a in r["aliases"].split("|") if a]) == 2 for r in raw_rows(db, "SELECT aliases FROM entities"))


def test_every_row_of_notes_appears_once_and_every_column_equals_the_database(tmp_path, db):
    out = export_json(tmp_path, db)
    want = raw_rows(db, "SELECT * FROM notes ORDER BY id")
    assert [o["id"] for o in out] == [w["id"] for w in want]           # once each, ordered by id
    assert len(want) == 13
    for o, w in zip(out, want):
        for col, value in w.items():
            assert o[col] == value and type(o[col]) is type(value), (w["id"], col)
        assert set(o) == set(w) | {"entities", "links"}                # nothing else is added


def test_the_five_old_keys_come_first_and_equal_the_old_commands_output(tmp_path, db):
    # what the command used to run, word for word
    old = [dict(zip(("id", "kind", "content", "keywords", "created_at"), tuple(r))) for r in sqlite3.connect(db).execute(
        "SELECT id, kind, content, keywords, created_at FROM notes WHERE status = 'active' ORDER BY id")]
    new = export_json(tmp_path, db, "--live-only")
    assert [{k: n[k] for k in ("id", "kind", "content", "keywords", "created_at")} for n in new] == old
    assert all(list(n)[:5] == ["id", "kind", "content", "keywords", "created_at"] for n in new)
    assert all(n["status"] == "active" for n in new) and len(new) == len(old)
    assert "trust" in new[0] and "entities" in new[0]                   # still every field


def test_every_link_appears_once_under_its_source_note(tmp_path, db):
    out = {o["id"]: o for o in export_json(tmp_path, db)}
    want = raw_rows(db, "SELECT src, dst, rel FROM note_links")
    assert len(want) >= 4
    got = [(o["id"], l["to"], l["rel"]) for o in out.values() for l in o["links"]]
    assert sorted(got) == sorted((w["src"], w["dst"], w["rel"]) for w in want)
    assert all(set(l) == {"to", "rel"} for o in out.values() for l in o["links"])


def test_each_notes_entities_match_the_entity_tables(tmp_path, db):
    out = {o["id"]: o for o in export_json(tmp_path, db)}
    for nid, o in out.items():
        want = sorted((r["name"], tuple(a for a in r["aliases"].split("|") if a)) for r in raw_rows(
            db, "SELECT e.name, e.aliases FROM note_entities ne JOIN entities e ON e.id = ne.entity_id WHERE ne.note_id = ?",
            (nid,)))
        assert sorted((e["name"], tuple(e["aliases"])) for e in o["entities"]) == want, nid
    orchid = [e for e in out[1]["entities"] if e["name"] == "Orchid"]
    assert orchid == [{"name": "Orchid", "aliases": ["orchid dashboard", "ORC"]}]


def test_live_only_gives_the_active_notes_with_every_field(tmp_path, db):
    live = export_json(tmp_path, db, "--live-only")
    assert [o["id"] for o in live] == [r["id"] for r in raw_rows(db, "SELECT id FROM notes WHERE status='active' ORDER BY id")]
    assert any(o["review"] == "pending" for o in live)                 # a pending note is still an active row


def test_a_status_the_exporter_has_never_heard_of_is_exported_too(tmp_path, db):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE notes SET status = 'archived' WHERE id = 13")
    out = {o["id"]: o for o in export_json(tmp_path, db)}
    assert out[13]["status"] == "archived"
    r = cli(tmp_path, "export", "--db", str(db), "--out", str(tmp_path / "e.json"))
    assert "archived 1" in r.stdout


def test_a_table_dump_is_identical_before_and_after_every_kind_of_export(tmp_path, db):
    before = table_dump(db)
    for flags in ((), ("--live-only",), ("--out", str(tmp_path / "x.json"))):
        assert cli(tmp_path, "export", "--db", str(db), *flags).returncode == 0
    assert table_dump(db) == before


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply POSIX file modes")
def test_out_is_mode_600_refuses_to_overwrite_and_force_overwrites(tmp_path, db):
    out = tmp_path / "e.json"
    r = cli(tmp_path, "export", "--db", str(db), "--out", str(out))
    assert r.returncode == 0
    assert r.stdout.strip() == f"exported 13 notes to {out} (active 9, merged 1, superseded 2, rejected 1)"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    first = out.read_bytes()
    r = cli(tmp_path, "export", "--db", str(db), "--out", str(out), "--live-only")
    assert r.returncode == 2 and "already exists" in r.stdout + r.stderr and "--force" in r.stdout + r.stderr
    assert out.read_bytes() == first                                    # untouched
    out.chmod(0o644)
    r = cli(tmp_path, "export", "--db", str(db), "--out", str(out), "--live-only", "--force")
    assert r.returncode == 0 and out.read_bytes() != first
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_the_json_result_line_counts_per_status(tmp_path, db):
    r = cli(tmp_path, "export", "--db", str(db), "--out", str(tmp_path / "e.json"), "--json")
    d = json.loads(r.stdout)
    assert d["ok"] and d["exported"] == 13
    assert d["by_status"] == {"active": 9, "merged": 1, "superseded": 2, "rejected": 1}


def test_help_says_what_is_not_exported(tmp_path):
    r = cli(tmp_path, "export", "--help")
    flat = " ".join(r.stdout.split())
    assert "Not exported: vectors, the search index, tombstones" in flat and "usage log" in flat


def test_an_encrypted_database_is_opened_the_way_search_opens_it(tmp_path):
    pytest.importorskip("sqlcipher3")
    path = tmp_path / "enc.db"
    env = {"HERMIND_DB_KEY": "correct horse battery staple"}
    assert cli(tmp_path, "remember", "Atlas uses PostgreSQL", "--db", str(path), env=env).returncode == 0
    r = cli(tmp_path, "export", "--db", str(path), env=env)
    assert r.returncode == 0 and json.loads(r.stdout)[0]["content"] == "Atlas uses PostgreSQL"
    r = cli(tmp_path, "export", "--db", str(path))                      # no key: one line, no traceback
    assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout
