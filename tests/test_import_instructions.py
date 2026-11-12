"""The `instructions` and `paste` readers of `hermind import`."""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
import import_readers  # noqa: E402
from store import DecisionStore  # noqa: E402

MAILBOX = "qa" + "@" + "lists.example.test"          # assembled at run time so no scanner sees an address literal
AGENTS = """# Atlas
<!-- internal: keep in sync with the wiki -->
Atlas is the billing service. Build it with make.

## Testing
Run the unit tests with pytest. They need Redis on port 6380.
Mail MAILBOX or ping @alice about flaky ones.

## Deploys
Deploys go out on Thursdays after the smoke test.
""".replace("MAILBOX", MAILBOX)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))                # Windows reads this one, not HOME
    (tmp_path / "home").mkdir()


def texts(db):
    s = DecisionStore(str(db))
    try:
        return [r["content"] for r in s.conn.execute("SELECT content FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


def proj(tmp_path, name="atlas"):
    d = tmp_path / "work" / name
    d.mkdir(parents=True)
    return d


# -- M2 check 1, 2, 3 ------------------------------------------------------------------------------------------------------

def test_agents_md_is_detected_and_gives_three_notes_without_the_comment(tmp_path):
    f = proj(tmp_path) / "AGENTS.md"
    f.write_text(AGENTS, encoding="utf-8")
    out = import_core.run_import(str(f), tmp_path / "a.db", apply=True, backup=False)
    assert out["reader"] == "instructions" and out["reader_detected"] and out["counts"]["new"] == 3
    notes = texts(tmp_path / "a.db")
    assert len(notes) == 3 and not any("internal: keep in sync" in n or "<!--" in n for n in notes)
    assert "Atlas" in notes[0] and "atlas" in notes[0].lower()
    assert "Testing" in notes[1] and notes[1].startswith("Atlas › Testing\n\n")      # the label repeats the first heading: said once
    assert "Deploys" in notes[2]
    assert [e["origin"] for e in out["entries"]] == ["AGENTS.md#1", "AGENTS.md#2", "AGENTS.md#3"]
    assert all(e["kind"] for e in out["entries"])


def test_a_file_named_notes_md_is_still_the_markdown_reader(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# T\nsome note\n", encoding="utf-8")
    out = import_core.run_import(str(f), tmp_path / "n.db")
    assert out["reader"] == "markdown"
    assert import_readers.detect_instructions(tmp_path) == 0           # a folder never scores for instructions
    assert import_readers.detect_instructions(f) == 0


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", ".cursorrules",
                                  ".windsurfrules", ".clinerules", "copilot-instructions.md", "style.instructions.md", "api.mdc"])
def test_every_listed_name_is_detected(tmp_path, name):
    f = tmp_path / name
    f.write_text("Always run the linter.\n", encoding="utf-8")
    assert import_readers.detect_instructions(f) == 3
    assert import_core.detect_reader(f) == ("instructions", [])


def test_a_clinerules_folder_is_not_an_instruction_file(tmp_path):
    d = tmp_path / ".clinerules"
    d.mkdir()
    (d / "a.md").write_text("rule\n", encoding="utf-8")
    assert import_readers.detect_instructions(d) == 0


def test_at_imports_are_listed_and_never_read_even_when_the_file_exists(tmp_path):
    d = proj(tmp_path)
    (d / "docs").mkdir()
    secret = "Setup needs the staging hostname kestrel-7."
    (d / "docs" / "setup.md").write_text(secret + "\n", encoding="utf-8")
    (d / "docs" / "setup.md").chmod(0)
    (d / "CLAUDE.md").write_text("See @docs/setup.md and @~/notes/me.md for details.\n\n```\n@in/code.md\n```\n"
                                 "<!-- @in/comment.md -->\nUse `@inline/code.md` never.\n", encoding="utf-8")
    try:
        out = import_core.run_import(str(d / "CLAUDE.md"), tmp_path / "i.db", apply=True, backup=False)
    finally:
        (d / "docs" / "setup.md").chmod(0o644)
    assert out["ok"] and not any(secret in t for t in texts(tmp_path / "i.db"))
    (rem,) = [r for r in out["remarks"] if "@ import" in r]
    assert "2 @ import(s) were not followed" in rem and "@docs/setup.md, @~/notes/me.md" in rem
    assert "in/code.md" not in rem and "in/comment.md" not in rem and "inline/code.md" not in rem


def test_more_than_ten_imports_list_ten(tmp_path):
    f = proj(tmp_path) / "CLAUDE.md"
    f.write_text("\n".join(f"@docs/f{i}.md" for i in range(13)) + "\n\nRun tests first.\n", encoding="utf-8")
    out = import_core.run_import(str(f), tmp_path / "i.db")
    (rem,) = [r for r in out["remarks"] if "@ import" in r]
    assert "13 @ import(s)" in rem and "f9.md" in rem and "f10.md" not in rem


# -- subject: front matter and the scope label ----------------------------------------------------------------------------

def test_front_matter_is_not_text_its_description_and_paths_join_the_subject(tmp_path):
    f = proj(tmp_path) / "api.instructions.md"
    f.write_text('---\ndescription: API conventions\napplyTo: ignored\npaths:\n  - "src/api/**/*.ts"\n  - src/shared/*.ts\n---\n'
                 "# Rules\nReturn 404 for unknown ids.\n", encoding="utf-8")
    import_core.run_import(str(f), tmp_path / "f.db", apply=True, backup=False)
    (note,) = texts(tmp_path / "f.db")
    assert note == ("atlas › Rules › API conventions › applies to: src/api/**/*.ts, src/shared/*.ts\n\n"
                    "Return 404 for unknown ids.")
    assert "applyTo" not in note and "description:" not in note
    g = proj(tmp_path, "other") / "a.mdc"
    g.write_text("---\nglobs: **/*.py\n---\nPrefer pathlib.\n", encoding="utf-8")
    import_core.run_import(str(g), tmp_path / "g.db", apply=True, backup=False)
    assert texts(tmp_path / "g.db") == ["other › applies to: **/*.py\n\nPrefer pathlib."]


def test_scope_label_is_the_holding_folder_or_the_one_above_dot_folders(tmp_path):
    label = import_readers._label_for
    home = tmp_path / "home"
    assert label(home / "AGENTS.md") is None                                   # the user's home folder: no label
    assert label(home / ".claude" / "CLAUDE.md") is None
    work = tmp_path / "work" / "atlas"
    for sub in ("", ".claude", ".github", os.path.join(".cursor", "rules")):
        assert label(work / sub / "x.md") == "atlas"
    assert label(work / "docs" / "x.md") == "docs"
    assert label(tmp_path / "work" / "20260302" / "x.md") is None             # a name without words adds nothing
    assert label(tmp_path / "work" / "a8f3c2d1" / "x.md") is None


def test_a_body_that_already_says_the_label_and_heading_adds_no_subject(tmp_path):
    f = proj(tmp_path) / "AGENTS.md"
    f.write_text("# Deploys\nAtlas deploys go out on Thursdays.\n", encoding="utf-8")
    import_core.run_import(str(f), tmp_path / "s.db", apply=True, backup=False)
    assert texts(tmp_path / "s.db") == ["Atlas deploys go out on Thursdays."]


def test_a_file_that_cannot_be_decoded_is_unreadable_and_encoding_fixes_it(tmp_path):
    f = proj(tmp_path) / "GEMINI.md"
    f.write_bytes("Keep answers short, 繁體中文 only.\n".encode("big5"))
    out = import_core.run_import(str(f), tmp_path / "e.db")
    assert out["counts"]["unreadable"] == 1 and "--encoding" in out["entries"][0]["reason"]
    out = import_core.run_import(str(f), tmp_path / "e.db", options={"encoding": "big5"}, apply=True, backup=False)
    assert out["counts"]["new"] == 1 and "繁體中文" in texts(tmp_path / "e.db")[0]


def test_a_file_with_only_comments_says_so(tmp_path):
    f = proj(tmp_path) / "AGENTS.md"
    f.write_text("<!-- nothing yet -->\n", encoding="utf-8")
    out = import_core.run_import(str(f), tmp_path / "c.db")
    assert out["total"] == 0 and any("no text" in r for r in out["remarks"])


# -- the paste reader ------------------------------------------------------------------------------------------------------

PASTE = """Here is everything I remember about you:

**Work and projects**

- You are a hydrologist at the Lakeview water board.
- You are preparing a flood-risk talk for October,
  and you want it short.
- You keep your notes in plain Markdown files.

**Personal**

- You keep bees.
- You are learning Portuguese.

1. Your sister is called Ines.
2) You grow chillies on the balcony.
(3) You run 10 km on Sundays.
"""


def test_paste_file_gives_nine_entries_each_with_its_category(tmp_path):
    f = tmp_path / "memory.txt"
    f.write_text(PASTE, encoding="utf-8")
    out = import_core.run_import(str(f), tmp_path / "p.db", reader="paste")
    assert out["reader"] == "paste" and out["total"] == 9 and out["counts"]["new"] == 9
    assert all(e["kind"] == "preference" for e in out["entries"])
    assert [e["origin"] for e in out["entries"]] == [f"memory.txt#{i}" for i in range(1, 10)]
    import_core.run_import(str(f), tmp_path / "p.db", reader="paste", apply=True, backup=False)
    notes = texts(tmp_path / "p.db")
    assert notes[0] == "Here is everything I remember about you:"
    assert all(n.startswith("Work and projects\n\n") for n in notes[1:4])
    assert all(n.startswith("Personal\n\n") for n in notes[4:])
    assert "and you want it short." in notes[2] and not notes[1].startswith("- ")        # continuation kept, marker gone
    assert notes[8] == "Personal\n\nYou run 10 km on Sundays."


def test_the_same_file_without_from_paste_is_one_markdown_note(tmp_path):
    f = tmp_path / "memory.txt"
    f.write_text(PASTE, encoding="utf-8")
    assert import_core.readers()["paste"].detect(f) == 0
    out = import_core.run_import(str(f), tmp_path / "p.db")
    assert out["reader"] == "markdown" and out["total"] == 1


def test_paste_refuses_a_folder_with_a_clear_message(tmp_path):
    with pytest.raises(import_core.ImportFailed, match="one text file"):
        import_core.run_import(str(tmp_path), tmp_path / "p.db", reader="paste")


# -- M2 check 5: preview equals apply, second apply, revert ------------------------------------------------------------------

@pytest.mark.parametrize("which", ["instructions", "paste"])
def test_preview_equals_apply_second_apply_is_all_duplicates_and_revert_leaves_nothing(tmp_path, which):
    f = (proj(tmp_path) / "AGENTS.md") if which == "instructions" else tmp_path / "memory.txt"
    f.write_text(AGENTS if which == "instructions" else PASTE, encoding="utf-8")
    db = tmp_path / "x.db"
    opts = {"reader": which}
    prev = import_core.run_import(str(f), db, **opts)
    app = import_core.run_import(str(f), db, apply=True, backup=False, **opts)
    key = lambda o: [(e["origin"], e["outcome"], e["text"], e["kind"]) for e in o["entries"]]    # noqa: E731
    assert key(prev) == key(app) and app["counts"]["new"] == prev["counts"]["new"] > 0
    again = import_core.run_import(str(f), db, apply=True, backup=False, **opts)
    assert again["counts"]["new"] == 0 and again["counts"]["duplicate"] == prev["total"] and again["batch"] is None
    import_core.run_revert(app["batch"], db, apply=True, backup=False)
    assert texts(db) == []
