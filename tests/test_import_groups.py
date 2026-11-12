"""Import core, part 3: the idle clock of imported notes, source groups, history with a reader's own reason."""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402

DAY = 86400.0
NOW = time.time()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def fake_reader(name, entries, groups=(), remarks=()):
    """Register a reader that returns fixed entries (the source argument is ignored)."""
    import_core.register(Reader(name, "test reader", lambda p: 0,
                                lambda src, view: ReadResult(entries=list(entries), remarks=list(remarks), groups=list(groups))))
    return name


def rows(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes ORDER BY id")]
    finally:
        s.close()


def test_imported_note_idle_clock_starts_at_the_import_and_native_notes_keep_theirs(tmp_path):
    db = tmp_path / "a.db"
    old = NOW - 2 * 365 * DAY
    text = "User is allergic to cashews."
    s = DecisionStore(str(db))
    s.add("User's sister lives in Porto and works there.", kind="chat", created_at=old)      # a native note, two years old
    s.close()
    fake_reader("t-idle", [Entry(text=text, origin="a#1", kind="chat", created=old)])
    out = import_core.run_import(str(tmp_path), db, reader="t-idle", apply=True, backup=False)
    assert out["counts"]["new"] == 1
    native, imported = rows(db)
    assert DecisionStore._idle_factor(imported, NOW) >= 0.99
    assert abs(imported["created_at"] - old) < 1 and imported["access_count"] == 0
    assert DecisionStore._idle_factor(native, NOW) < 0.4 and native["last_access"] is None
    assert imported["expires_at"] is None


def test_every_reader_starts_the_idle_clock_the_markdown_and_hermes_ones_too(tmp_path):
    src = tmp_path / "notes"
    src.mkdir()
    (src / "a.md").write_text("---\ncreated: 2023-01-05\nkind: chat\n---\nThe spare key is under the blue pot.\n", encoding="utf-8")
    db = tmp_path / "m.db"
    import_core.run_import(str(src), db, apply=True, backup=False)
    (n,) = rows(db)
    assert n["created_at"] < NOW - 365 * DAY and DecisionStore._idle_factor(n, NOW) >= 0.99
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "USER.md").write_text("User keeps bees.\n", encoding="utf-8")
    db2 = tmp_path / "h.db"
    import_core.run_import(str(mem), db2, apply=True, backup=False)
    (n2,) = rows(db2)
    assert n2["last_access"] is not None and n2["access_count"] == 0


def test_glob_escape_matches_only_the_literal_name():
    import fnmatch
    for name in ("plain", "a[b]c", "star*name", "what?", "[x]", "a]b"):
        pat = import_core.glob_escape(name)
        assert fnmatch.fnmatchcase(name, pat)
        assert not fnmatch.fnmatchcase(name + "z", pat)
    assert import_core.glob_escape("a[b]") == "a[[]b[]]" and import_core.glob_escape("*?") == "[*][?]"
    assert not fnmatch_case("axc", import_core.glob_escape("a*c"))


def fnmatch_case(name, pat):
    import fnmatch
    return fnmatch.fnmatchcase(name, pat)


ENTRIES = [Entry(text="Alpha fact one.", origin="one/a#1"), Entry(text="Alpha fact two.", origin="one/a#2"),
           Entry(text="Beta fact one.", origin="two/b#1"), Entry(text="", origin="two/b#2"),
           Entry(text="Loose fact.", origin="loose#1")]
GROUPS = [("first part", "one/*"), ("second part", "two/*")]


def test_groups_first_match_wins_other_comes_last_and_counts_add_up(tmp_path):
    fake_reader("t-groups", ENTRIES, GROUPS)
    out = import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-groups")
    assert [g["name"] for g in out["groups"]] == ["first part", "second part", "other"]
    assert [g["exclude"] for g in out["groups"]] == ["one/*", "two/*", None]
    assert [g["total"] for g in out["groups"]] == [2, 2, 1]
    assert out["groups"][1]["counts"]["empty"] == 1 and out["groups"][1]["counts"]["new"] == 1
    for k in import_core.OUTCOMES:
        assert sum(g["counts"][k] for g in out["groups"]) == out["counts"][k]
    assert [e["group"] for e in out["entries"]] == ["first part"] * 2 + ["second part"] * 2 + ["other"]


def test_group_without_stray_entries_has_no_other_and_excluding_a_group_changes_only_it(tmp_path):
    fake_reader("t-groups2", ENTRIES[:4], GROUPS)
    base = import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-groups2")
    assert [g["name"] for g in base["groups"]] == ["first part", "second part"]
    for g in base["groups"]:
        out = import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-groups2", exclude=[g["exclude"]])
        for e0, e1 in zip(base["entries"], out["entries"]):
            if e0["group"] == g["name"]:
                assert e1["outcome"] == "excluded"
            else:
                assert (e1["outcome"], e1["reason"]) == (e0["outcome"], e0["reason"])
        assert out["groups"][[x["name"] for x in out["groups"]].index(g["name"])]["counts"]["excluded"] == g["total"]


def test_group_counts_follow_the_final_outcomes_after_apply(tmp_path):
    fake_reader("t-groups3", ENTRIES, GROUPS)
    db = tmp_path / "g.db"
    out = import_core.run_import(str(tmp_path), db, reader="t-groups3", apply=True, backup=False)
    assert out["groups"][0]["counts"]["new"] == 2 and out["created"] == 4
    again = import_core.run_import(str(tmp_path), db, reader="t-groups3", apply=True, backup=False)
    assert again["groups"][0]["counts"]["duplicate"] == 2 and again["counts"]["new"] == 0


def test_text_output_has_a_by_source_section_with_the_exclude_patterns(tmp_path):
    fake_reader("t-groups4", ENTRIES, [("first [x]", "one/" + import_core.glob_escape("[x]") + "*"), ("second part", "two/*")])
    out = import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-groups4")
    text = hermind_cli.import_text(out)
    assert "By source:" in text
    assert "second part: 1 new, 1 empty   (leave out: --exclude 'two/*')" in text
    assert "--exclude 'one/[[]x[]]*'" in text and "other: 3 new" in text
    one = import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-groups4", exclude=["two/*", "loose*"])
    # a single remaining group of a reader with one group prints no section
    fake_reader("t-single", ENTRIES[:2], [("only", "one/*")])
    assert "By source" not in hermind_cli.import_text(import_core.run_import(str(tmp_path), tmp_path / "g.db", reader="t-single"))
    assert one["counts"]["excluded"] == 3


def test_a_reader_marks_an_older_version_as_history_with_its_own_reason(tmp_path):
    fake_reader("t-hist", [Entry(text="Old wording.", origin="h#1", history="removed in the app on 2026-03-01"),
                           Entry(text="Current wording.", origin="h#2")])
    db = tmp_path / "h.db"
    out = import_core.run_import(str(tmp_path), db, reader="t-hist", apply=True, backup=False)
    assert out["counts"]["history"] == 1 and out["counts"]["new"] == 1
    e = out["entries"][0]
    assert (e["outcome"], e["reason"]) == ("history", "removed in the app on 2026-03-01")
    assert [r["content"] for r in rows(db)] == ["Current wording."]


def test_hermind_export_history_keeps_its_own_reason(tmp_path):
    src = tmp_path / "n"
    src.mkdir()
    (src / "old.md").write_text("---\nstatus: superseded\n---\nThe gate code is 4411.\n", encoding="utf-8")
    out = import_core.run_import(str(src), tmp_path / "x.db")
    e = out["entries"][0]
    assert e["outcome"] == "history" and "an older version kept for history (status: superseded)" in e["reason"]
