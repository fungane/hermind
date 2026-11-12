"""The block splitter (text_blocks.py): one test per rule of its spec, and the two worked examples."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import text_blocks as tb  # noqa: E402
from text_blocks import SEP, split_blocks  # noqa: E402

F = "`" * 3
LIGHTHOUSE = f"""# Lighthouse
<!-- maintainers: keep this short -->
Lighthouse is the sensor dashboard.

## Testing
- Unit tests run with npm.
- E2E tests need the fake broker on port 1884.

{F}
npm run test:unit
{F}

**Deploys**

1. Build the image.
2. Push it to the registry.
"""
CLAUDE_MEMORY = """**Work context**
Ravi is a hydrologist at the Lakeview water board.

**Personal context**
Ravi keeps bees and is learning Portuguese.

**Top of mind**
He is preparing a flood-risk talk for October.

**Brief history**

*Recent months*

Ravi asked for help cleaning river-gauge data.

*Earlier context*

He moved from Pune two years ago.
"""


def view(text, **kw):
    return [(SEP.join(b.path), b.text) for b in split_blocks(text, **kw)]


def test_lighthouse_example_without_item_mode():
    got = view(LIGHTHOUSE)
    assert [p for p, _ in got] == ["Lighthouse", "Lighthouse › Testing", "Lighthouse › Testing › Deploys"]
    assert got[0][1] == "Lighthouse is the sensor dashboard."                      # the comment is gone
    assert "- Unit tests run with npm." in got[1][1] and "- E2E tests" in got[1][1] and "npm run test:unit" in got[1][1]
    assert got[2][1] == "1. Build the image.\n2. Push it to the registry."
    # NOTE the spec's example lists this last path as "Lighthouse › Deploys"; its own rule ("a bold-only line sits below
    # every ATX level") gives the path asserted above. The report names this conflict.


def test_lighthouse_example_in_item_mode_has_five_blocks():
    got = view(LIGHTHOUSE, items=True)
    assert len(got) == 5
    assert [t for p, t in got if p == "Lighthouse"] == ["Lighthouse is the sensor dashboard."]
    testing = [t for p, t in got if p == "Lighthouse › Testing"]
    assert testing[0] == "Unit tests run with npm."                                  # the marker is not part of the block
    assert testing[1].startswith("E2E tests need the fake broker") and "npm run test:unit" in testing[1]   # the code joins the second
    assert "npm run" not in testing[0]
    assert [t for p, t in got if p.endswith("Deploys")] == ["Build the image.", "Push it to the registry."]


def test_claude_memory_text_gives_five_blocks_and_none_is_a_heading():
    got = view(CLAUDE_MEMORY)
    assert [p for p, _ in got] == ["Work context", "Personal context", "Top of mind", "Brief history › Recent months",
                                   "Brief history › Earlier context"]
    assert all(not t.startswith("**") and not t.startswith("*") for _, t in got)


def test_line_ends_and_byte_order_mark():
    assert view("﻿# A\r\nbody one\r\nstill one\r\n\r\nbody two\r") == [("A", "body one\nstill one"), ("A", "body two")]


def test_html_comments_single_inline_and_multi_line():
    text = "intro <!-- gone --> kept\n<!-- a\nmultiple\nline\ncomment -->\nnext line\n\n<!--x--> tail"
    assert view(text) == [("", "intro  kept\nnext line"), ("", " tail")]


def test_fence_is_opaque_nothing_inside_is_heading_item_or_comment():
    text = f"# T\n\n{F}md\n# not a heading\n- not an item\n<!-- not a comment -->\n\n**nor bold**\n{F}\n"
    got = view(text, items=True)
    assert got == [("T", f"{F}md\n# not a heading\n- not an item\n<!-- not a comment -->\n\n**nor bold**\n{F}")]
    assert view("~~~\n# x\n~~~\nafter") == [("", "~~~\n# x\n~~~\nafter")]


def test_fence_after_blank_line_joins_the_block_before_under_the_same_path_else_stands_alone():
    joined = view(f"# T\ntext\n\n{F}\ncode\n{F}\n")
    assert joined == [("T", f"text\n\n{F}\ncode\n{F}")]
    alone = view(f"# T\ntext\n\n# U\n\n{F}\ncode\n{F}\n")
    assert alone == [("T", "text"), ("U", f"{F}\ncode\n{F}")]
    assert view(f"{F}\ncode\n{F}\n") == [("", f"{F}\ncode\n{F}")]
    assert view(f"text\n---\n\n{F}\ncode\n{F}\n") == [("", "text"), ("", f"{F}\ncode\n{F}")]


def test_unclosed_fence_runs_to_the_end_and_a_longer_fence_needs_a_long_close():
    assert view(f"a\n{F}\ncode\nmore") == [("", f"a\n{F}\ncode\nmore")]
    four = "`" * 4
    assert view(f"{four}\n{F}\ninner\n{F}\n{four}\nz") == [("", f"{four}\n{F}\ninner\n{F}\n{four}\nz")]


def test_what_is_a_heading_and_what_is_not():
    h = tb.heading_of
    assert h("# Title") == (1, "Title") and h("###### Deep ##") == (6, "Deep")
    assert h("#hashtag") is None and h("####### seven") is None
    assert h("**Work context**") == (7, "Work context") and h("__Work__") == (7, "Work")
    assert h("**Work:**") == (7, "Work") and h("**Work**:") == (7, "Work")
    assert h("*Recent months*") == (8, "Recent months") and h("_Recent_") == (8, "Recent")
    assert h("**Why:** the tests were slow") is None
    assert h("**one** and **two**") is None and h("*one* and *two*") is None
    assert h("plain text") is None and h("- **Bold item**") is None


def test_heading_path_is_a_stack():
    text = ("# A\n## B\n### C\nc1\n\n## D\nd1\n\n# E\ne1\n\n**bold**\nb1\n\n*ital*\ni1\n\n**bold2**\nb2\n\n# F\nf1\n\n"
            "**x**\nx1\n\n## G\ng1\n")
    assert [p for p, _ in view(text)] == ["A › B › C", "A › D", "E", "E › bold", "E › bold › ital", "E › bold2", "F",
                                          "F › x", "F › G"]


def test_a_bold_line_is_below_every_atx_level_and_an_italic_below_a_bold_one():
    got = view("# A\n#### D\nd\n\n**B**\nb\n\n*I*\ni\n")
    assert [p for p, _ in got] == ["A › D", "A › D › B", "A › D › B › I"]


def test_rules_end_a_block_and_are_not_text():
    assert view("one\n---\ntwo\n***\nthree\n___\nfour") == [("", "one"), ("", "two"), ("", "three"), ("", "four")]


def test_a_heading_never_makes_a_block_and_an_empty_section_gives_nothing():
    assert view("# A\n## B\n\n# C\nbody\n# D\n") == [("C", "body")]
    assert view("# only\n") == [] and view("") == []


def test_a_heading_line_inside_a_run_of_lines_splits_it():
    assert view("line one\n## H\nline two") == [("", "line one"), ("H", "line two")]


def test_item_mode_markers_continuations_and_nested_items():
    text = ("intro line\nsecond intro line\n- one\n  continues here\n  - nested\n* two\n+ three\n• four\n1. five\n2) six\n"
            "(3) seven\n\nplain paragraph\n")
    got = [t for _, t in view(text, items=True)]
    assert got == ["intro line\nsecond intro line", "one\ncontinues here\n- nested", "two", "three", "four", "five", "six",
                   "seven", "plain paragraph"]
    # without item mode a list is one block
    assert len(view(text)) == 2


def test_item_mode_bold_line_is_not_a_marker():
    assert view("**Cat**\n- a\n- b\n", items=True) == [("Cat", "a"), ("Cat", "b")]


def test_subject_line_uses_label_and_path_only_when_the_body_lacks_them():
    assert tb.subject_line("atlas", ("Setup", "Redis"), "Port is 6380") == f"atlas{SEP}Setup{SEP}Redis"
    assert tb.subject_line("atlas", ("Setup",), "The Atlas setup needs Redis.") == ""
    assert tb.subject_line(None, ("Setup", "Redis"), "Redis port is 6380, set up by hand") == ""
    assert tb.subject_line("atlas", ("Atlas", "Redis"), "Port is 6380") == f"Atlas{SEP}Redis"      # a repeat is said once, as the heading wrote it
    assert tb.note_text("Port is 6380", None, ("Redis",)) == "Redis\n\nPort is 6380"
    assert tb.note_text("Redis port is 6380", None, ("Redis",)) == "Redis port is 6380"
    assert tb.note_text("Port", None, ()) == "Port"
