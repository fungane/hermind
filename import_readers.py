"""Readers for `hermind import`: each turns one kind of source into entries for the import core (import_core.py).

A reader is one function ``read(source: Path, view) -> ReadResult`` plus one ``register(Reader(...))`` call at the
bottom of this file. *view* answers read-only questions about the target database (a reader that must recognise its
own earlier exports needs it); most readers ignore it. Anything a reader sees and does not turn into an entry goes
into ``ReadResult.remarks``, so the preview can say it.
"""

from __future__ import annotations

from pathlib import Path

import fnmatch
import os
import re

try:
    from . import md_notes, text_blocks
    from .import_core import Entry, ImportFailed, Reader, ReadResult, register
    from .screen import strip_injected
    from .store_common import IMPORT_HERMIND
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import md_notes
    import text_blocks
    from import_core import Entry, ImportFailed, Reader, ReadResult, register
    from screen import strip_injected
    from store_common import IMPORT_HERMIND


def _is_hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


# -- Markdown: a folder of text notes (.md .markdown .mdx .txt), or one such file -------------------------------------------

# front matter keys the reader understands; every other key is named in a remark (with the number of files that had it)
MD_KEYS = {"kind", "keywords", "aliases", "tags", "valid_from", "valid_to", "created", "date", "status", "title", "name",
           "description", "summary", "pinned", "trust", "scope", "id", "rev"}
MD_BOOKKEEPING = ("supersedes", "outdated_by")     # Hermind's own export writes them: only worth a word when not empty


def md_entry(meta: dict, body: str, origin: str, stem: str = "", *, ignore_file_names: bool = False,
             label: str | None = None) -> Entry:
    """The entry for one parsed note file. The text is the body with its subject in front when the body does not say
    it (see md_notes.describe); the body alone is kept for the rules that must judge it. Metadata that cannot be used
    is passed on as it is: the core drops it and says so."""
    kw = [str(k) for k in md_notes.as_list(meta.get("keywords")) + md_notes.as_list(meta.get("aliases")) if str(k).strip()]
    created = meta.get("created")
    if created in (None, ""):
        created = meta.get("date")
    created = str(created).strip() if created not in (None, "") else None
    status = str(meta.get("status") or "").strip().lower()
    scope = str(meta.get("scope") or "").strip() or None
    own = meta.get("id") is not None and bool(meta.get("rev"))        # a file of Hermind's own export carries id and rev
    return Entry(text=md_notes.describe(meta, body, stem, use_file_name=not ignore_file_names, label=label), body=body, origin=origin,
                 kind=str(meta.get("kind") or "").strip().lower() or None, keywords=" ".join(kw), created=created,
                 tags=md_notes.tag_list(meta.get("tags")), pinned=meta.get("pinned") is True,
                 valid_from=meta.get("valid_from") or None, valid_to=meta.get("valid_to") or None,
                 pending=status == "pending", status=status if status in ("superseded", "merged") else "",
                 trust=meta.get("trust"), scope=scope, source_session=IMPORT_HERMIND if own else None)


def unused_keys(meta: dict) -> list[str]:
    return [k for k in meta if k not in MD_KEYS and not (k in MD_BOOKKEEPING and not meta[k])]


def read_markdown(source: Path, view, encoding: str | None = None, ignore_file_names: bool = False) -> ReadResult:
    res = ReadResult()
    if source.is_dir():
        root, walk = source, md_notes.walk_folder(source)
        files = walk.notes
        if walk.remark():
            res.remarks.append(walk.remark())
    else:
        root, files = source.parent, [source]
    unused: dict[str, int] = {}
    for path in files:
        origin = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            res.entries.append(Entry(text="", origin=origin, unreadable=f"cannot read the file ({exc.strerror or exc})"))
            continue
        text, why = md_notes.decode_text(data, encoding)
        if text is None:
            res.entries.append(Entry(text="", origin=origin, unreadable=why))
            continue
        meta, body = md_notes.parse_note_file(text)
        for k in unused_keys(meta):
            unused[k] = unused.get(k, 0) + 1
        entry = md_entry(meta, body, origin, path.stem, ignore_file_names=ignore_file_names)
        versions = view.note_versions(meta["id"]) if md_notes.int_id(meta) is not None and meta.get("rev") else []
        note = md_notes.owner_of(meta, versions)
        if note is not None:
            entry.owner_id = note["id"]
            try:
                norm = {k: md_notes.day10(meta[k]) for k in ("valid_from", "valid_to") if k in meta}
                db_f, f, changed = md_notes.compare_file(note, {**meta, **norm}, body)
            except ValueError:
                changed = ["valid_from/valid_to"]
                f = None
            entry.owner_relation = "edited" if changed else "same"
            if changed:
                older = f is not None and md_notes.rev_of(f) == str(meta["rev"])
                entry.owner_detail = "an older version of the note" if older else "differs in: " + ", ".join(changed)
        res.entries.append(entry)
    if unused:
        res.remarks.append("front matter keys that are not used (their values were left out of the notes): " + ", ".join(
            f"{k} ({n} file{'s' if n != 1 else ''})" for k, n in sorted(unused.items(), key=lambda kv: (-kv[1], kv[0]))))
    return res


def detect_markdown(source: Path) -> int:
    if source.is_dir():
        return 2 if md_notes.walk_folder(source).notes else 0
    return 2 if source.suffix.lower() in md_notes.TEXT_SUFFIXES else 0


register(Reader("markdown", "a folder of text notes (.md, .markdown, .mdx, .txt: Hermind's own export, or any notes; one file is one "
                "note) or a single such file", detect_markdown, read_markdown, options=("encoding", "ignore_file_names")))


# -- Hermes built-in memory: MEMORY.md (the agent's notes) and USER.md (the user profile) ------------------------------------

HERMES_FILES = {"MEMORY.MD": ("memory", "state"), "USER.MD": ("user", "preference")}   # upper-cased file name -> (target, kind)


def _hermes_files(source: Path) -> list[Path]:
    if source.is_dir():
        return [p for p in sorted(source.iterdir()) if p.is_file() and p.name.upper() in HERMES_FILES]
    return [source] if source.name.upper() in HERMES_FILES else []


def split_entries(text: str) -> list[str]:
    """The entries of a built-in memory file: separated by a line that contains only §."""
    entries, cur = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n"):
        if line.strip() == "§":
            entries.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    entries.append("\n".join(cur))
    return entries


def read_hermes_builtin(source: Path, view) -> ReadResult:
    """Each entry becomes one note written exactly as on_memory_write (in __init__.py) writes its live mirror: the
    same source_session ("builtin:user" / "builtin:memory"), kind (preference / state) and pinned flag, and the same
    cleaning of recalled-memory blocks, so a later edit or removal through the built-in memory tool finds the note."""
    res = ReadResult()
    files = _hermes_files(source)
    if source.is_dir():
        others = [p.name for p in sorted(source.iterdir()) if p.is_file() and p not in files]
        if others:
            res.remarks.append(f"{len(others)} other file(s) in the folder were ignored: {', '.join(others[:5])}"
                               f"{', ...' if len(others) > 5 else ''}")
        if not files:
            res.remarks.append("this folder has no MEMORY.md and no USER.md")
    for path in files:
        target, kind = HERMES_FILES[path.name.upper()]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            res.remarks.append(f"{path.name}: not read ({type(exc).__name__}); it is not a UTF-8 text file")
            continue
        for n, raw in enumerate(split_entries(text), 1):
            res.entries.append(Entry(text=strip_injected(raw), origin=f"{path.name}#{n}", kind=kind, pinned=True,
                                     source_session=f"builtin:{target}"))
    return res


def detect_hermes_builtin(source: Path) -> int:
    files = _hermes_files(source)
    if not files:
        return 0
    if not source.is_dir():
        return 3
    others = [p for p in source.rglob("*.md") if p not in files and not _is_hidden(p, source)]
    return 2 if others else 3      # a folder that also holds other notes is ambiguous: --from decides


register(Reader("hermes-builtin", "Hermes' built-in memory: MEMORY.md and USER.md (entries separated by a line with only §), "
                "a folder holding them or one of the files",
                detect_hermes_builtin, read_hermes_builtin))


# -- instruction files of coding agents (AGENTS.md, CLAUDE.md, GEMINI.md, ...) and pasted lists ----------------------------

INSTRUCTION_NAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", ".cursorrules",
                     ".windsurfrules", ".clinerules", "copilot-instructions.md")
INSTRUCTION_PATTERNS = ("*.instructions.md", "*.mdc")
_IMPORT = re.compile(r"(?<![\w@/.])@((?:~|\.{1,2})?/?[\w.\-]+(?:/[\w.\-]+)*(?:/)?)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
SHOWN = 5                       # names a remark lists
SHOWN_IMPORTS = 10              # @ imports a remark lists


def is_instruction_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return path.name in INSTRUCTION_NAMES or any(fnmatch.fnmatchcase(path.name.lower(), p) for p in INSTRUCTION_PATTERNS)


def detect_instructions(source: Path) -> int:
    return 3 if is_instruction_file(source) else 0


def _names(items, shown: int = SHOWN) -> str:
    items = list(items)
    return ", ".join(items[:shown]) + (f", ... ({len(items)} in all)" if len(items) > shown else "")


def at_imports(text: str) -> list[str]:
    """The ``@path`` imports in *text*, as written, outside code and comments (Claude Code's import syntax)."""
    found: list[str] = []
    for m in _IMPORT.finditer(_CODE_SPAN.sub(" ", text_blocks.prose(text))):
        raw = m.group(0).rstrip(".,;:!?)")
        path = raw[1:]
        if ("/" in path or "." in path.strip(".")) and raw not in found:      # a path, not a mention (@alice)
            found.append(raw)
    return found


def _label_for(path: Path) -> str | None:
    """The scope label of an instruction file: the name of the folder that holds it (the folder above for .claude/,
    .github/ and .cursor/rules/). None for the user's home folder and for a name without words."""
    here = Path(os.path.abspath(path)).parent
    if here.name in (".claude", ".github"):
        here = here.parent
    elif here.name == "rules" and here.parent.name == ".cursor":
        here = here.parent.parent
    try:
        if Path(os.path.abspath(here)) == Path(os.path.abspath(Path.home())):
            return None
    except RuntimeError:
        pass
    name = here.name
    return name if name and md_notes.has_words(name) else None


def _applies_to(meta: dict) -> str | None:
    for key in ("paths", "globs"):
        v = meta.get(key)
        items = [str(x).strip() for x in md_notes.as_list(v) if str(x).strip()]
        if len(items) == 1 and "," in items[0]:
            items = [x.strip() for x in items[0].split(",") if x.strip()]
        if items:
            return "applies to: " + ", ".join(items)
    return None


def instruction_entries(text: str, origin_prefix: str, label: str | None) -> tuple[list[Entry], list[str]]:
    """The entries of one instruction file's text (front matter included) and the ``@`` imports it names. The entries'
    origins are ``<origin_prefix>#<n>``; the subject of each is the scope label, the heading path, the description and
    the paths the file applies to."""
    top, nested, body = md_notes.parse_front(text)
    meta = {**nested, **top}
    extra = []
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip():
        extra.append(" ".join(desc.split()))
    applies = _applies_to(meta)
    if applies:
        extra.append(applies)
    entries = []
    for n, b in enumerate(text_blocks.split_blocks(body), 1):
        entries.append(Entry(text=text_blocks.note_text(b.text, label, b.path, extra), body=b.text,
                             origin=f"{origin_prefix}#{n}"))
    return entries, at_imports(body)


def _read_text(path: Path, encoding: str | None, origin: str | None = None) -> tuple[str | None, Entry | None]:
    """(text, None) or (None, an ``unreadable`` entry) for a text file."""
    origin = origin or path.name
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, Entry(text="", origin=origin, unreadable=f"cannot read the file ({exc.strerror or exc})")
    text, why = md_notes.decode_text(data, encoding)
    return (text, None) if text is not None else (None, Entry(text="", origin=origin, unreadable=why))


def read_instructions(source: Path, view, encoding: str | None = None) -> ReadResult:
    res = ReadResult()
    text, bad = _read_text(source, encoding)
    if bad is not None:
        res.entries.append(bad)
        return res
    entries, imports = instruction_entries(text, source.name, _label_for(source))
    res.entries.extend(entries)
    if not entries:
        res.remarks.append(f"{source.name} has no text outside comments and front matter")
    if imports:
        res.remarks.append(f"{len(imports)} @ import(s) were not followed (the imported files are not read): "
                           + _names(imports, SHOWN_IMPORTS))
    return res


register(Reader("instructions", "one instruction file of a coding agent (AGENTS.md, CLAUDE.md, GEMINI.md, .cursorrules, "
                "copilot-instructions.md, *.instructions.md, *.mdc ...): one note per block under a heading",
                detect_instructions, read_instructions, options=("encoding",)))


def read_paste(source: Path, view, encoding: str | None = None) -> ReadResult:
    res = ReadResult()
    if source.is_dir():
        raise ImportFailed(f"{source}: --from paste reads one text file, not a folder")
    text, bad = _read_text(source, encoding)
    if bad is not None:
        res.entries.append(bad)
        return res
    for n, b in enumerate(text_blocks.split_blocks(text, items=True), 1):
        res.entries.append(Entry(text=text_blocks.note_text(b.text, None, b.path), body=b.text, kind="preference",
                                 origin=f"{source.name}#{n}"))
    return res


register(Reader("paste", "a text file holding what an assistant answered when asked to list everything it remembers about "
                "you (only with --from paste): one note per list item, with its category", lambda p: 0, read_paste,
                options=("encoding",)))


try:                       # the readers that live in their own modules register themselves when imported (keep these last)
    from . import reader_claude_code, reader_exports, reader_hermind_json  # noqa: E402,F401
except ImportError:
    import reader_claude_code  # noqa: E402,F401
    import reader_exports  # noqa: E402,F401
    import reader_hermind_json  # noqa: E402,F401
