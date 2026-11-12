"""SPEC-4 M2: `hermind export --format jsonl`, entity lines with observations and relation lines."""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_fixture import build_db, table_dump  # noqa: E402
from test_export_json import cli, raw_rows  # noqa: E402

PENDING = "(waiting for review)"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "a.db"
    ids = build_db(path)
    with sqlite3.connect(path) as con:             # a note whose validity ends in the future must carry no marker
        con.execute("UPDATE notes SET valid_to = ? WHERE id = ?", (time.time() + 400 * 86400, ids["plain"]))
    return path


IDS = {"edited": 1, "old_edit": 2, "merged_keep": 3, "merged_drop": 4, "old_state": 5, "new_state": 6, "stale_old": 7,
       "stale_new": 8, "rejected": 9, "pending": 10, "pinned": 11, "rich": 12, "plain": 13}


def export(tmp_path, db, *flags):
    r = cli(tmp_path, "export", "--db", str(db), "--format", "jsonl", *flags)
    assert r.returncode == 0, r.stderr + r.stdout
    return [json.loads(l) for l in r.stdout.splitlines()]


def entities(lines):
    return {l["name"]: l for l in lines if l["type"] == "entity"}


def name(i):
    return f"hermind-note-{i}"


def test_one_entity_line_per_active_note_and_more_with_history(tmp_path, db):
    live = entities(export(tmp_path, db))
    active = [r["id"] for r in raw_rows(db, "SELECT id FROM notes WHERE status = 'active' ORDER BY id")]
    assert list(live) == [name(i) for i in active] and len(active) == 9      # the pending note is one of them
    allv = entities(export(tmp_path, db, "--include-history"))
    older = [r["id"] for r in raw_rows(db, "SELECT id FROM notes WHERE status IN ('superseded', 'merged') ORDER BY id")]
    assert set(allv) == set(live) | {name(i) for i in older} and len(older) == 3
    assert name(IDS["rejected"]) not in allv                                   # a rejected note is not an older version


def test_every_line_is_valid_json_with_the_documented_keys(tmp_path, db):
    for flags in ((), ("--include-history",)):
        for l in export(tmp_path, db, *flags):
            if l["type"] == "entity":
                assert list(l) == ["type", "name", "entityType", "observations", "hermind"]
                assert isinstance(l["observations"], list) and len(l["observations"]) == 1
                assert l["hermind"]["id"] == int(l["name"].rsplit("-", 1)[1])
                assert l["entityType"] == l["hermind"]["kind"]
                assert {"entities", "links", "status", "trust"} <= set(l["hermind"])
            else:
                assert list(l) == ["type", "from", "to", "relationType"] and l["type"] == "relation"


def test_the_hermind_part_is_the_full_record_of_the_json_export(tmp_path, db):
    full = {o["id"]: o for o in json.loads(cli(tmp_path, "export", "--db", str(db)).stdout)}
    for l in entities(export(tmp_path, db, "--include-history")).values():
        assert l["hermind"] == full[l["hermind"]["id"]]


def test_every_marker_is_on_exactly_the_notes_it_applies_to(tmp_path, db):
    lines = entities(export(tmp_path, db, "--include-history"))
    text = {int(n.rsplit("-", 1)[1]): l["observations"][0] for n, l in lines.items()}
    pending = {i for i, t in text.items() if t.startswith(PENDING)}
    outdated = {i for i, t in text.items() if "(outdated: " in t}
    expired = {i for i, t in text.items() if "(no longer valid after " in t}
    older = {i for i, t in text.items() if "(an older version of " in t}
    assert pending == {IDS["pending"]}
    assert outdated == {IDS["stale_old"]} and f"(outdated: {name(IDS['stale_new'])} is newer)" in text[IDS["stale_old"]]
    # the rich note's end date is in the past; two older versions stopped being valid when they were replaced
    assert expired == {IDS["rich"], 2, IDS["old_state"]}
    assert "(no longer valid after 2024-12-31)" in text[IDS["rich"]]
    assert IDS["plain"] not in expired                                          # a future valid_to is not a marker
    assert older == {2, IDS["merged_drop"], IDS["old_state"]}
    assert f"(an older version of {name(IDS['edited'])}, kept for history)" in text[2]            # canonical_id
    assert f"(an older version of {name(IDS['merged_keep'])}, kept for history)" in text[IDS["merged_drop"]]
    assert f"(an older version of {name(IDS['new_state'])}, kept for history)" in text[IDS["old_state"]]
    for i in set(text) - pending - outdated - expired - older:                  # unmarked notes are their own text
        assert not text[i].startswith("(")


def test_markers_are_in_front_of_the_text_in_the_documented_order(tmp_path, db):
    lines = entities(export(tmp_path, db, "--include-history"))
    t = lines[name(2)]["observations"][0]                                       # an old version of the edited note
    assert re.match(r"^\(no longer valid after \d{4}-\d{2}-\d{2}\) \(an older version of hermind-note-1, kept for history\) "
                    r"Priya keeps the Orchid dashboard on PostgreSQL\.$", t)
    with sqlite3.connect(db) as con:                                            # a note that is all four at once
        con.execute("UPDATE notes SET review = 'pending', valid_to = 1000000000 WHERE id = 4")
        con.execute("INSERT INTO note_links(src, dst, rel, created_at) VALUES (4, 8, 'stale_by', 1)")
    t = entities(export(tmp_path, db, "--include-history"))[name(4)]["observations"][0]
    assert t.startswith("(outdated: hermind-note-8 is newer) (no longer valid after 2001-09-09) (an older version of "
                        "hermind-note-3, kept for history) Nebula")
    assert PENDING not in t                                                     # an old version is not itself waiting


def test_an_older_version_finds_its_note_by_link_when_canonical_id_is_missing(tmp_path, db):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE notes SET canonical_id = NULL WHERE id = 2")
    t = entities(export(tmp_path, db, "--include-history"))[name(2)]["observations"][0]
    assert "(an older version of hermind-note-1, kept for history)" in t
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM note_links WHERE src = 2")
    t = entities(export(tmp_path, db, "--include-history"))[name(2)]["observations"][0]
    assert "(an older version, kept for history)" in t


def test_relation_lines_join_only_exported_notes(tmp_path, db):
    for flags in ((), ("--include-history",)):
        lines = export(tmp_path, db, *flags)
        ids = set(entities(lines))
        rel = [l for l in lines if l["type"] == "relation"]
        assert all(l["from"] in ids and l["to"] in ids for l in rel)
        db_links = {(name(r["src"]), name(r["dst"]), r["rel"]) for r in raw_rows(db, "SELECT src, dst, rel FROM note_links")}
        want = {l for l in db_links if l[0] in ids and l[1] in ids}
        assert {(l["from"], l["to"], l["relationType"]) for l in rel} == want and len(rel) == len(want)
    assert len([l for l in export(tmp_path, db) if l["type"] == "relation"]) == 1               # only stale_by
    assert len([l for l in export(tmp_path, db, "--include-history") if l["type"] == "relation"]) == 4


def test_exporting_twice_gives_byte_identical_files(tmp_path, db):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    before = table_dump(db)
    for flags in ((), ("--include-history",)):
        for f in (a, b):
            f.unlink(missing_ok=True)
            assert cli(tmp_path, "export", "--db", str(db), "--format", "jsonl", "--out", str(f), *flags).returncode == 0
        assert a.read_bytes() == b.read_bytes() and a.stat().st_size > 0
    assert table_dump(db) == before                                             # export only reads


def test_out_and_force_work_as_for_json(tmp_path, db):
    out = tmp_path / "e.jsonl"
    r = cli(tmp_path, "export", "--db", str(db), "--format", "jsonl", "--out", str(out))
    assert r.returncode == 0 and "9 notes and 1 relations to" in r.stdout and oct(out.stat().st_mode & 0o777) == "0o600"
    r = cli(tmp_path, "export", "--db", str(db), "--format", "jsonl", "--out", str(out))
    assert r.returncode == 2 and "--force" in r.stdout + r.stderr
    assert cli(tmp_path, "export", "--db", str(db), "--format", "jsonl", "--out", str(out), "--force").returncode == 0


def test_text_with_line_separator_characters_stays_on_one_line(tmp_path, db):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE notes SET content = 'one two\u0085three four\nfive' WHERE id = 13")
    r = cli(tmp_path, "export", "--db", str(db), "--format", "jsonl")
    assert len(r.stdout.splitlines()) == len(r.stdout.split("\n")) - 1 == 10
    got = entities([json.loads(l) for l in r.stdout.splitlines()])[name(13)]["observations"][0]
    assert got == "one two\u0085three four\nfive"
