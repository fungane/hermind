"""The block splitter of `hermind import`: a Markdown-ish text becomes blocks, each with its heading path.

Used by the readers of instruction files, Claude Code, Claude's export and pasted lists (import_readers.py).

* Line ends are normalised, a byte-order mark is dropped, HTML comments are removed (Claude Code strips them before its
  model sees an instruction file).
* Fenced code (``` or ~~~) is opaque: nothing inside is a heading, a list item or a comment, and a fence is never split.
  A fence that follows a blank line belongs to the block before it when that block has the same heading path; otherwise
  it is a block of its own.
* A heading is an ATX line (``# Title``), a line made only of one bold span (``**Work context**``, ``__Work__``, with an
  optional colon) or a line made only of one italic span (``*Recent months*``). The heading path is a stack: an ATX
  heading of level n replaces the levels from n down, a bold-only line sits below every ATX level and an italic-only
  line below a bold one.
* A block is a run of non-blank lines that are not headings. ``---``, ``***`` and ``___`` lines end a block and are not
  text. A heading never makes a block of its own.
* Item mode (pasted lists, MEMORY.md): every top-level list item is a block of its own, with its continuation lines and
  nested items and without its own marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEP = " › "

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(\S.*?)(?:[ \t]+#+)?[ \t]*$")
_BOLD = re.compile(r"^[ \t]*(\*\*|__)(?!\s)(.+?)(?<!\s)\1[ \t]*:?[ \t]*$")
_ITALIC = re.compile(r"^[ \t]*(\*|_)(?!\1|\s)(.+?)(?<!\s)\1[ \t]*:?[ \t]*$")
_RULE = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_ITEM = re.compile(r"^([ \t]*)(?:[-*+•]|\d+[.)]|\(\d+\))[ \t]+(.*)$")
_BOLD_LEVEL, _ITALIC_LEVEL = 7, 8


@dataclass
class Block:
    path: tuple[str, ...]      # the heading path above the block, outermost first
    text: str                  # the block's own text (the entry's ``body``)


def _drop_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """*line* without its HTML comment parts; the second value says whether a comment is still open at the line end."""
    out, i = [], 0
    while i < len(line):
        if in_comment:
            end = line.find("-->", i)
            if end == -1:
                return "".join(out), True
            i, in_comment = end + 3, False
        else:
            start = line.find("<!--", i)
            if start == -1:
                out.append(line[i:])
                break
            out.append(line[i:start])
            i, in_comment = start + 4, True
    return "".join(out), in_comment


def _tokens(text: str) -> list[tuple[str, object]]:
    """("line", s) for a text line (blank lines too) and ("fence", [lines]) for a whole fenced block."""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    toks: list[tuple[str, object]] = []
    fence: list[str] | None = None
    mark, in_comment = "", False
    for raw in text.split("\n"):
        if fence is not None:
            fence.append(raw.rstrip())
            s = raw.strip()
            if s.startswith(mark) and not s.strip(mark[0]):          # a closing fence: the same character, at least as long
                toks.append(("fence", fence))
                fence = None
            continue
        line, still = _drop_comments(raw, in_comment)
        touched = in_comment or line != raw
        in_comment = still
        if touched and not line.strip():                                # nothing but a comment: the line is gone
            continue
        m = _FENCE.match(line)
        if m:
            fence, mark = [line.rstrip()], m.group(1)
            continue
        toks.append(("line", line.rstrip()))
    if fence is not None:                                               # an unclosed fence runs to the end of the text
        toks.append(("fence", fence))
    return toks


def prose(text: str) -> str:
    """*text* without its HTML comments and fenced code: what is left is text that may name an ``@path`` import."""
    out = []
    for kind, val in _tokens(text):
        if kind == "line":
            out.append(val)                                              # type: ignore[arg-type]
    return "\n".join(out)


def _clean_heading(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(\*\*|__|\*|_)(.+?)\1$", r"\2", t).strip()
    return t.rstrip(":").strip()


def heading_of(line: str) -> tuple[int, str] | None:
    """(level, text) when *line* is a heading, else None. Levels 1-6 are ATX, 7 is bold-only, 8 is italic-only."""
    m = _ATX.match(line)
    if m:
        text = _clean_heading(m.group(2))
        return (len(m.group(1)), text) if text else None
    m = _BOLD.match(line)
    if m and m.group(1) not in m.group(2):
        text = _clean_heading(m.group(2))
        return (_BOLD_LEVEL, text) if text else None
    m = _ITALIC.match(line)
    if m and m.group(1) not in m.group(2):
        text = _clean_heading(m.group(2))
        return (_ITALIC_LEVEL, text) if text else None
    return None


def split_blocks(text: str, *, items: bool = False) -> list[Block]:
    stack: list[tuple[int, str]] = []
    blocks: list[Block] = []
    cur: list[str] = []
    joinable: int | None = None        # the last block under the current path: a fence after a blank line joins it
    item_indent: int | None = None     # indentation of the first list item of the current paragraph (item mode)
    item_offset = 0                    # where the text of the current item starts, to dedent its continuation lines

    def path() -> tuple[str, ...]:
        return tuple(t for _, t in stack)

    def flush() -> None:
        nonlocal cur, joinable
        body = "\n".join(cur).strip("\n")
        cur = []
        if body.strip():
            blocks.append(Block(path(), body))
            joinable = len(blocks) - 1

    for kind, val in _tokens(text):
        if kind == "fence":
            lines = "\n".join(val)                                       # type: ignore[arg-type]
            if cur:
                cur.append(lines)
            elif joinable is not None and blocks[joinable].path == path():
                blocks[joinable].text += "\n\n" + lines
            else:
                cur.append(lines)                                        # a block of its own (lines right after it join it)
            continue
        line = val                                                       # type: ignore[assignment]
        if not line.strip():
            flush()
            item_indent = None
            continue
        if _RULE.match(line):
            flush()
            joinable = None
            item_indent = None
            continue
        h = heading_of(line)
        if h is not None:
            flush()
            level, title = h
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            joinable = None
            item_indent = None
            continue
        if items:
            m = _ITEM.match(line)
            indent = len(m.group(1).expandtabs(4)) if m else 0
            if m and (item_indent is None or indent <= item_indent):
                flush()
                item_indent = indent if item_indent is None else min(item_indent, indent)
                item_offset = len(line) - len(m.group(2))
                cur.append(m.group(2))
                continue
            if cur and item_indent is not None:                          # a continuation line or a nested item
                lead = len(line) - len(line.lstrip())
                cur.append(line[min(lead, item_offset):])
                continue
        cur.append(line)
    flush()
    return blocks


# -- the subject of a block --------------------------------------------------------------------------------------

def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower())


def subject_line(label: str | None, path: tuple[str, ...], body: str, extra: tuple[str, ...] | list[str] = ()) -> str:
    """The scope label and the heading path joined with ' › ', each piece only when its squashed form (lower-cased, no
    punctuation or spacing) is not inside the squashed body: a block that already says it needs no subject. A piece that
    repeats the one before it is said once, in the later wording."""
    flat = _squash(body)
    pieces: list[str] = []
    for piece in ([label] if label else []) + list(path) + list(extra):
        sq = _squash(piece)
        if sq and sq not in flat:
            if pieces and _squash(pieces[-1]) == sq:
                pieces[-1] = piece                    # a repeat: the later wording (a heading) replaces the earlier (a label)
            else:
                pieces.append(piece)
    return SEP.join(pieces)


def note_text(body: str, label: str | None, path: tuple[str, ...], extra: tuple[str, ...] | list[str] = ()) -> str:
    """The note text of a block: the subject line, a blank line and the block (just the block when it says it all).
    *extra* are further subject pieces after the heading path (a file's description, what it applies to)."""
    subject = subject_line(label, path, body, extra)
    return f"{subject}\n\n{body}" if subject else body
