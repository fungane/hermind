"""The ``claude-code`` reader of `hermind import`: Claude Code's configuration folder (usually ~/.claude), one project's
memory folder, or a MEMORY.md index.

What it reads: every project's memory folder, subagent memory, and the instruction files Claude Code loads (CLAUDE.md,
CLAUDE.local.md, rules). What it never opens: session transcripts (*.jsonl), credentials, and any file it has no rule
for; those are counted in remarks. Nothing is written, renamed or extracted. There is no default source path.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    from . import md_notes, text_blocks
    from .import_core import Entry, Reader, ReadResult, glob_escape, register
    from .import_readers import MD_KEYS, SHOWN, _names, _read_text, at_imports, instruction_entries, md_entry
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import md_notes
    import text_blocks
    from import_core import Entry, Reader, ReadResult, glob_escape, register
    from import_readers import MD_KEYS, SHOWN, _names, _read_text, at_imports, instruction_entries, md_entry

ENCODED = re.compile(r"^(?:-|[A-Za-z]--)")                 # how Claude Code names a project folder after a path
INDEX_LINE = re.compile(r"^[ \t]*[-*+][ \t]+\[(?P<title>[^\]]+)\]\((?P<file>[^)\s]+\.md)\)"
                        r"(?:(?: — | - |: )(?P<summary>.*?))?[ \t]*$")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+\.md)\)")
KNOWN_KEYS = {"metadata", "node_type", "originSessionId", "modified", "type"}        # never named in the unused-keys remark
TYPE_KINDS = {"user": "preference", "feedback": "preference", "project": "tech", "reference": "tech"}
MEMORY_TYPES = set(TYPE_KINDS)
DEFAULT_ROOT = Path(os.sep)                                 # where the project folder search starts (a test seam)


# -- finding a project's folder from the name Claude Code gave its memory ----------------------------------------------

def encode(name: str) -> str:
    """Every character other than an ASCII letter or digit becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def find_project_folder(encoded: str, root: Path | str | None = None) -> tuple[Path | None, int]:
    """(the folder, 1) when exactly one folder on this computer has a path that encodes to *encoded*; (None, n) with n the
    number of candidates found (0, or 2 for "more than one") otherwise. The walk starts at *root* (default: the file
    system root) and follows only folder entries whose encoded name matches the next part of the name; a folder that
    cannot be listed is skipped."""
    if encoded.startswith("-"):
        start, rest = Path(root) if root is not None else DEFAULT_ROOT, encoded
    elif re.match(r"^[A-Za-z]--", encoded):
        start, rest = (Path(root) if root is not None else Path(encoded[0] + ":\\")), encoded[2:]
    else:
        return None, 0
    found: list[Path] = []

    def walk(cur: Path, remaining: str) -> None:
        if len(found) > 1:
            return
        if not remaining:
            found.append(cur)
            return
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            piece = "-" + encode(e.name)
            if remaining == piece or remaining.startswith(piece + "-"):
                walk(Path(e.path), remaining[len(piece):])

    walk(start, rest)
    return (found[0], 1) if len(found) == 1 else (None, len(found))


# -- recognising what a source is --------------------------------------------------------------------------------------

def _front_type(path: Path) -> tuple[bool, bool]:
    """(is a memory topic file, has front matter at all) from the first 4 KiB of a .md file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "ignore")
    except OSError:
        return False, False
    top, nested, _ = md_notes.parse_front(head + ("\n---\n" if head.startswith("---\n") and "\n---" not in head[3:] else ""))
    meta = {**nested, **top}
    memory = meta.get("node_type") == "memory" or str(meta.get("type") or "").strip().lower() in MEMORY_TYPES
    return memory, bool(meta)


def has_index_lines(text: str) -> bool:
    return any(INDEX_LINE.match(ln) for ln in text.replace("\r\n", "\n").split("\n"))


def _memory_md_text(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(1_000_000).decode("utf-8", "ignore").lstrip("﻿")
    except OSError:
        return None


def _is_index_file(path: Path) -> bool:
    """A MEMORY.md with index lines and no § separator lines (that one is Hermes' built-in memory)."""
    text = _memory_md_text(path)
    return text is not None and has_index_lines(text) and not any(ln.strip() == "§" for ln in text.split("\n"))


def is_config_folder(folder: Path) -> bool:
    try:
        projects = folder / "projects"
        if projects.is_dir() and any(p.is_dir() and ENCODED.match(p.name) for p in projects.iterdir()):
            return True
        return (folder / "CLAUDE.md").is_file() and (folder / "settings.json").is_file()
    except OSError:
        return False


def is_memory_folder(folder: Path) -> bool:
    try:
        index = folder / "MEMORY.md"
        if index.is_file() and _is_index_file(index):
            return True
        return any(p.suffix.lower() == ".md" and p.is_file() and _front_type(p)[0] for p in sorted(folder.iterdir())[:200])
    except OSError:
        return False


def detect_claude_code(source: Path) -> int:
    try:
        if source.is_dir():
            return 4 if is_config_folder(source) or is_memory_folder(source) else 0
        return 4 if source.name == "MEMORY.md" and _is_index_file(source) else 0
    except OSError:
        return 0


# -- reading a memory folder -------------------------------------------------------------------------------------------

class _State:
    """What the reader saw and did not turn into entries (the remarks), over the whole source."""

    def __init__(self) -> None:
        self.unused: dict[str, int] = {}
        self.other_files: dict[str, int] = {}
        self.imports: list[str] = []
        self.remarks: list[str] = []

    def other(self, name: str) -> None:
        ext = Path(name).suffix.lower() or "(no extension)"
        self.other_files[ext] = self.other_files.get(ext, 0) + 1


def _topic_files(folder: Path, st: _State) -> list[Path]:
    """The .md files of a memory folder except its MEMORY.md index (sorted); other files are counted, not read."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = here / name
            if path.suffix.lower() == ".md":
                if not (here == folder and name == "MEMORY.md"):
                    out.append(path)
            else:
                st.other(name)
    return sorted(out)


def _read_memory_folder(folder: Path, label: str | None, prefix: str, st: _State, *, with_topics: bool = True) -> list[Entry]:
    """The entries of one memory folder: its MEMORY.md (index lines that link to a topic file give that file keywords,
    the rest becomes notes) and one note per topic file. Origins are ``<prefix><path inside the folder>``."""
    entries: list[Entry] = []
    topics = _topic_files(folder, st) if with_topics else []
    rel = {p.relative_to(folder).as_posix(): p for p in topics}
    index_info: dict[str, tuple[str, str]] = {}           # topic file (relative path) -> (title, summary) of its index line
    index = folder / "MEMORY.md"
    index_entries: list[Entry] = []
    if index.is_file():
        text, bad = _read_text(index, None, prefix + "MEMORY.md")
        if bad is not None:
            index_entries.append(bad)
        else:
            kept: list[str] = []
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                m = INDEX_LINE.match(line)
                if m:
                    target = os.path.normpath(m.group("file")).replace(os.sep, "/")
                    if target in rel:
                        index_info.setdefault(target, (m.group("title").strip(), (m.group("summary") or "").strip()))
                        continue                            # the file's own note carries this line
                    line = LINK.sub(lambda k: k.group(1), line)         # a missing file: the line itself becomes a note
                kept.append(line)
            for n, b in enumerate(text_blocks.split_blocks("\n".join(kept), items=True), 1):
                index_entries.append(Entry(text=text_blocks.note_text(b.text, label, b.path), body=b.text,
                                           origin=f"{prefix}MEMORY.md#{n}"))
            st.imports.extend(i for i in at_imports("\n".join(kept)) if i not in st.imports)
    entries.extend(index_entries)
    if not with_topics and index.is_file():
        siblings = sum(1 for p in folder.iterdir() if p.suffix.lower() == ".md" and p.name != "MEMORY.md")
        if siblings:
            st.remarks.append(f"{siblings} topic file(s) next to MEMORY.md were not read (only the one file was given): "
                              f"point at the folder to import them; every index line became a note")
    for path in topics:
        relpath = path.relative_to(folder).as_posix()
        origin = prefix + relpath
        text, bad = _read_text(path, None, origin)
        if bad is not None:
            entries.append(bad)
            continue
        top, nested, body = md_notes.parse_front(text)
        meta = {**nested, **top}                            # a top-level key wins over one nested under `metadata:`
        for k in meta:
            if k not in MD_KEYS and k not in KNOWN_KEYS:
                st.unused[k] = st.unused.get(k, 0) + 1
        kind = str(meta.get("kind") or "").strip().lower() or TYPE_KINDS.get(str(meta.get("type") or "").strip().lower(), "")
        created = next((meta[k] for k in ("modified", "created", "date") if meta.get(k) not in (None, "")), None)
        title, summary = index_info.get(relpath, ("", ""))
        extra_kw = [x for x in (title, summary) if x]
        meta = {**meta, "kind": kind or None, "created": created,
                "keywords": md_notes.as_list(meta.get("keywords")) + extra_kw}
        meta.pop("date", None)
        if title and not any(meta.get(k) not in (None, "") for k in ("title", "name")):
            meta["title"] = title                           # the index title is the subject of a file that has none
        entry = md_entry(meta, body, origin, path.stem, label=label)
        entries.append(entry)
    return entries


def _memory_folder_label(folder: Path, root) -> str | None:
    """The label of a single memory folder ``<x>/projects/<P>/memory``: the project folder's name, or P."""
    folder = Path(os.path.abspath(folder))
    if folder.name == "memory" and folder.parent.parent.name == "projects" and ENCODED.match(folder.parent.name):
        p = folder.parent.name
        found, _ = find_project_folder(p, root)
        return found.name if found else (p[1:] if p.startswith("-") else p)
    return None


# -- reading a configuration folder -----------------------------------------------------------------------------------

def _instruction_file(path: Path, origin_prefix: str, label: str | None, st: _State) -> list[Entry]:
    text, bad = _read_text(path, None, origin_prefix)
    if bad is not None:
        return [bad]
    entries, imports = instruction_entries(text, origin_prefix, label)
    st.imports.extend(i for i in imports if i not in st.imports)
    return entries


def _md_files(folder: Path, st: _State) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                out.append(Path(dirpath) / name)
            else:
                st.other(name)
    return sorted(out)


def _subdirs(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return []


def _auto_memory_directory(settings: Path, st: _State) -> Path | None:
    """``autoMemoryDirectory`` of settings.json (an absolute path or one that starts with ``~/``), or None."""
    try:
        data = json.loads(settings.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    except ValueError:
        st.remarks.append("settings.json is not valid JSON: its autoMemoryDirectory (if any) was not looked at")
        return None
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    if value.startswith("~/") or value == "~":
        return Path(os.path.expanduser(value))
    if os.path.isabs(value):
        return Path(value)
    st.remarks.append(f"autoMemoryDirectory in settings.json is not an absolute path ({value}): that folder was not read")
    return None


def read_claude_code(source: Path, view, *, root: Path | str | None = None) -> ReadResult:
    res = ReadResult()
    st = _State()
    if source.is_file():                                      # a MEMORY.md on its own
        label = _memory_folder_label(source.parent, root)
        res.entries = _read_memory_folder(source.parent, label, "", st, with_topics=False)
        res.groups = [(f"memory · {label}" if label else "memory", "*")]
        return _finish(res, st)
    if not is_config_folder(source):
        label = _memory_folder_label(source, root)
        res.entries = _read_memory_folder(source, label, "", st)
        res.groups = [(f"memory · {label}" if label else "memory", "*")]
        return _finish(res, st)
    return _finish(_read_config_folder(source, st, res, root), st)


def _read_config_folder(S: Path, st: _State, res: ReadResult, root) -> ReadResult:
    transcripts: list[str] = []
    not_found: list[str] = []
    agents_skipped: list[str] = []
    other_in_projects: list[str] = []
    found_any = False
    read_dirs: set[Path] = set()

    # -- the projects: label, folder, memory -----------------------------------------------------------------------
    projects: list[dict] = []
    for pdir in _subdirs(S / "projects"):
        if not ENCODED.match(pdir.name):
            other_in_projects.append(f"projects/{pdir.name}")
            continue
        folder, n = find_project_folder(pdir.name, root)
        projects.append({"P": pdir.name, "dir": pdir, "folder": folder, "n": n})
    names = [p["folder"].name for p in projects if p["folder"] is not None]
    for p in projects:
        fallback = p["P"][1:] if p["P"].startswith("-") else p["P"]
        p["label"] = p["folder"].name if p["folder"] is not None and names.count(p["folder"].name) == 1 else fallback
        if p["folder"] is None:
            not_found.append(p["label"])
        else:
            found_any = True

    def section(group: str, pattern: str, entries: list[Entry]) -> None:
        if entries:
            res.entries.extend(entries)
            res.groups.append((group, pattern))

    for p in projects:                                        # project memory folders (and what else is inside a project)
        try:
            items = sorted(p["dir"].iterdir(), key=lambda x: x.name)
        except OSError:
            items = []
        for item in items:
            if item.name == "memory" and item.is_dir():
                continue
            if item.is_file() and item.name.endswith(".jsonl"):
                transcripts.append(item.name)
            else:
                other_in_projects.append(f"{p['dir'].name}/{item.name}")
        mem = p["dir"] / "memory"
        if mem.is_dir():
            read_dirs.add(Path(os.path.realpath(mem)))
            section(f"memory · {p['label']}", f"memory/{glob_escape(p['label'])}/*",
                    _read_memory_folder(mem, p["label"], f"memory/{p['label']}/", st))

    for a in _subdirs(S / "agent-memory"):                    # user-scope subagent memory
        read_dirs.add(Path(os.path.realpath(a)))
        section(f"agent memory · {a.name}", f"agent-memory/{glob_escape(a.name)}/*",
                _read_memory_folder(a, a.name, f"agent-memory/{a.name}/", st))

    user_entries: list[Entry] = []                            # user instructions: CLAUDE.md and rules
    if (S / "CLAUDE.md").is_file():
        user_entries += _instruction_file(S / "CLAUDE.md", "user-instructions/CLAUDE.md", None, st)
    for path in _md_files(S / "rules", st) if (S / "rules").is_dir() else []:
        user_entries += _instruction_file(path, "user-instructions/" + path.relative_to(S).as_posix(), None, st)
    section("user instructions", "user-instructions/*", user_entries)

    for p in projects:                                        # the project folder's own instruction files
        d, label = p["folder"], p["label"]
        if d is None:
            continue
        files = [d / "CLAUDE.md", d / ".claude" / "CLAUDE.md", d / "CLAUDE.local.md"]
        present = [f for f in files if f.is_file()]
        if not present:
            present = [f for f in (d / "AGENTS.md", d / ".claude" / "AGENTS.md") if f.is_file()]
        elif any(f.is_file() for f in (d / "AGENTS.md", d / ".claude" / "AGENTS.md")):
            agents_skipped.append(label)
        if (d / ".claude" / "rules").is_dir():
            present += _md_files(d / ".claude" / "rules", st)
        ents: list[Entry] = []
        for f in present:
            ents += _instruction_file(f, f"project-instructions/{label}/{f.relative_to(d).as_posix()}", label, st)
        section(f"instructions · {label}", f"project-instructions/{glob_escape(label)}/*", ents)

    for p in projects:                                        # a project's subagent memory
        d, label = p["folder"], p["label"]
        if d is None:
            continue
        spots = ((".claude/agent-memory", "project-agent-memory"), (".claude/agent-memory-local", "project-agent-memory-local"))
        for a in sorted({x.name for sub, _ in spots for x in _subdirs(d / sub)}):
            ents = []
            for sub, where in spots:
                folder = d / sub / a
                if folder.is_dir():
                    read_dirs.add(Path(os.path.realpath(folder)))
                    ents += _read_memory_folder(folder, f"{label}/{a}", f"{where}/{label}/{a}/", st)
            section(f"agent memory · {label}/{a}", f"project-agent-memory*/{glob_escape(label)}/{glob_escape(a)}/*", ents)

    custom = _auto_memory_directory(S / "settings.json", st) if (S / "settings.json").is_file() else None
    if custom is not None:                                    # the folder settings.json names for automatic memory
        if not custom.is_dir():
            st.remarks.append(f"autoMemoryDirectory in settings.json names a folder that does not exist ({custom}): nothing read from it")
        elif Path(os.path.realpath(custom)) in read_dirs:
            st.remarks.append("autoMemoryDirectory in settings.json is a memory folder that was already read: not read twice")
        else:
            section("memory · custom folder", "custom-memory/*", _read_memory_folder(custom, None, "custom-memory/", st))

    # -- remarks -----------------------------------------------------------------------------------------------------
    if transcripts:
        st.remarks.append(f"{len(transcripts)} session transcript(s) (.jsonl) were not read: they are conversations, not memory "
                          f"({_names(transcripts)})")
    if not_found:
        st.remarks.append(f"{len(not_found)} project(s) have no folder on this computer (or more than one folder fits): their memory "
                          f"was read, their instruction files and subagent memory could not be ({_names(not_found)})")
    if agents_skipped:
        st.remarks.append(f"{len(agents_skipped)} project(s) have an AGENTS.md that was not read because a CLAUDE.md exists there "
                          f"(Claude Code's own rule) ({_names(agents_skipped)})")
    known = {"CLAUDE.md", "rules", "settings.json", "agent-memory", "projects"}
    try:
        others = sorted(x.name + ("/" if x.is_dir() else "") for x in S.iterdir() if x.name not in known)
    except OSError:
        others = []
    if others:
        st.remarks.append(f"{len(others)} other item(s) of the configuration folder were not read: agents, skills, commands, plans "
                          f"and the like are definitions, not memory; credentials are never opened ({_names(others)})")
    if other_in_projects:
        st.remarks.append(f"{len(other_in_projects)} other item(s) inside the projects folder were not read ({_names(other_in_projects)})")
    if found_any:
        st.remarks.append("CLAUDE.md files in a project's subfolders are not searched")
    return res


def _finish(res: ReadResult, st: _State) -> ReadResult:
    """Add the remarks that every source form shares (imports, other files, unused front matter keys)."""
    extra: list[str] = []
    if st.imports:
        extra.append(f"{len(st.imports)} @ import(s) were not followed: the imported files are not read ({_names(st.imports)})")
    if st.other_files:
        n = sum(st.other_files.values())
        extra.append(f"{n} file(s) that are not .md notes were not read inside memory folders and rules ("
                     + ", ".join(f"{c} {e}" for e, c in sorted(st.other_files.items(), key=lambda kv: (-kv[1], kv[0]))) + ")")
    if st.unused:
        extra.append("front matter keys that are not used (their values were left out of the notes): " + ", ".join(
            f"{k} ({n} file{'s' if n != 1 else ''})" for k, n in sorted(st.unused.items(), key=lambda kv: (-kv[1], kv[0]))))
    res.remarks = st.remarks + extra
    return res


register(Reader("claude-code", "Claude Code's configuration folder (usually ~/.claude; give the path yourself), one project's "
                "memory folder, or a MEMORY.md index: every project's memory, subagent memory and the CLAUDE.md / rules files "
                "it loads; session transcripts and credentials are never opened", detect_claude_code, read_claude_code))
