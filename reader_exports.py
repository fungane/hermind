"""The readers of `hermind import` for the data exports of ChatGPT (``chatgpt-export``) and Claude (``claude-export``).

Neither format is published. The shapes read here come from third-party tools that read them and were tested on synthetic
exports only; no real export has been seen.

A source is the export's ``.zip`` (read in place with ``zipfile``: nothing is extracted, written or renamed) or the folder
it was unpacked into. Account files (``user.json``, ``users.json``) are never opened, and neither is any file a reader has
no rule for: they are named or counted in a remark.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

try:
    from . import text_blocks
    from .import_core import Entry, ImportFailed, Reader, ReadResult, register
    from .import_readers import SHOWN, _names
    from .store_common import _digest as _store_digest
    from .store_writes import check_text
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import text_blocks
    from import_core import Entry, ImportFailed, Reader, ReadResult, register
    from import_readers import SHOWN, _names
    from store_common import _digest as _store_digest
    from store_writes import check_text

HEAD = 64 * 1024                   # how much of a conversation file detection looks at
NAMED_FILES = 20                   # files of the export's top folder a remark names


# -- a source: a ZIP read in place, or a folder -----------------------------------------------------------------------

class ExportSource:
    """The files of an export, by name (posix paths below its root). Reads on demand; never extracts anything."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.zf: zipfile.ZipFile | None = None
        self.sizes: dict[str, int] = {}
        self.members: dict[str, str] = {}     # name as listed -> the member name inside the ZIP (a leading ./ dropped)
        if path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = 0
                    self.sizes[full.relative_to(path).as_posix()] = size
        elif path.is_file() and zipfile.is_zipfile(path):
            self.zf = zipfile.ZipFile(path)
            for info in self.zf.infolist():
                if not info.is_dir():
                    name = info.filename
                    while name.startswith("./"):
                        name = name[2:]
                    self.sizes[name] = info.file_size
                    self.members[name] = info.filename
        else:
            raise ImportFailed(f"{path} is neither a ZIP file nor a folder: give the export's .zip or the folder it was unpacked into")
        self.names = sorted(self.sizes)

    def open(self, name: str):
        """A binary stream of one file."""
        if self.zf is not None:
            return self.zf.open(self.members.get(name, name))
        return open(self.path / name, "rb")

    def head(self, name: str, n: int = HEAD) -> str:
        with self.open(name) as fh:
            return fh.read(n).decode("utf-8", "ignore")

    def read_bytes(self, name: str) -> bytes:
        with self.open(name) as fh:
            return fh.read()

    def close(self) -> None:
        if self.zf is not None:
            self.zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_source(path: Path) -> ExportSource | None:
    try:
        return ExportSource(path)
    except (ImportFailed, OSError, zipfile.BadZipFile):
        return None


def iter_json_array(stream, step: int = 1 << 20):
    """The elements of a JSON list one at a time, so that a file of many conversations never sits in memory whole. Raises
    ValueError for text that is not a list or is cut off; elements yielded before that point are still good."""
    dec = json.JSONDecoder()
    buf, pos, eof = "", 0, False

    def more(n: int) -> None:
        nonlocal buf, pos, eof
        data = stream.read(n)
        if not data:
            eof = True
        else:
            buf, pos = buf[pos:] + data, 0

    def skip(chars: str) -> None:
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in chars:
                pos += 1
            if pos < len(buf) or eof:
                return
            more(step)

    skip(" \t\r\n﻿")
    if pos >= len(buf) or buf[pos] != "[":
        raise ValueError("the file is not a JSON list")
    pos += 1
    while True:
        skip(" \t\r\n,")
        if pos >= len(buf):
            raise ValueError("the JSON list is cut off")
        if buf[pos] == "]":
            return
        grow = step
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
                break
            except ValueError as exc:
                if eof:
                    raise ValueError(f"not valid JSON ({exc})") from None
                grow *= 2
                more(grow)
        pos = end
        yield obj


def text_stream(raw):
    return io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")


def _day(ts) -> str:
    """The UTC day of an epoch time, or 'undated'."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts))) if ts is not None else "undated"
    except (TypeError, ValueError, OverflowError, OSError):
        return "undated"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _file_remarks(src: ExportSource, read: set[str], never: tuple[str, ...], res: ReadResult) -> None:
    """Account for every file of the export that was not read: the top folder's files by name (an account file marked
    'never opened'), the rest counted by extension."""
    top = [n for n in src.names if n not in read and "/" not in n]
    deep = [n for n in src.names if n not in read and "/" in n]
    if top:
        shown = [f"{n} (never opened)" if n in never else n for n in top[:NAMED_FILES]]
        res.remarks.append(f"{_plural(len(top), 'other file')} of the export {'was' if len(top) == 1 else 'were'} not read: "
                           + ", ".join(shown) + (f", ... ({len(top)} in all)" if len(top) > NAMED_FILES else ""))
    if deep:
        by_ext: dict[str, int] = {}
        for n in deep:
            ext = Path(n).suffix.lower() or "(no extension)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
        res.remarks.append(f"{_plural(len(deep), 'file')} in folders of the export {'was' if len(deep) == 1 else 'were'} not read ("
                           + ", ".join(f"{c} {e}" for e, c in sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))) + ")")


# -- ChatGPT ----------------------------------------------------------------------------------------------------------

CONVERSATION_FILE = re.compile(r"^conversations(?:-\d+)?\.json$")
LIST_ITEM = re.compile(r"^\s*(\d+)\.\s+(?:\[(\d{4}-\d{2}-\d{2})\]\.?\s*)?(.*)$")
SUBJECT = "ChatGPT custom instructions"


def conversation_files(src: ExportSource) -> list[str]:
    """conversations.json or conversations-000.json, -001.json ... at most two folders deep, in a fixed order."""
    out = [n for n in src.names if n.count("/") <= 2 and CONVERSATION_FILE.match(n.rsplit("/", 1)[-1])]
    return sorted(out)


def detect_chatgpt(source: Path) -> int:
    src = open_source(source)
    if src is None:
        return 0
    try:
        return 4 if any('"mapping"' in src.head(n) for n in conversation_files(src)[:3]) else 0
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return 0
    finally:
        src.close()


def parse_memory_list(text: str) -> list[tuple[str | None, str]]:
    """The items of a memory list as (date or None, text). Items are lines ``N. [YYYY-MM-DD]. text`` (the date and the
    dot after ``]`` optional); a line that does not start an item continues the one before; when no line is numbered, each
    non-empty line is an item."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    if not any(LIST_ITEM.match(ln) for ln in lines):
        return [(None, ln.strip()) for ln in lines if ln.strip()]
    items: list[list] = []
    for ln in lines:
        m = LIST_ITEM.match(ln)
        if m:
            items.append([m.group(2), m.group(3).strip()])
        elif ln.strip():
            if items:
                items[-1][1] += "\n" + ln.strip()
            else:
                items.append([None, ln.strip()])
    return [(d, t) for d, t in items if t]


def _fenced(value) -> str:
    """The text inside the last ``` fence of *value*, or the whole value when it has no fence."""
    if not isinstance(value, str):
        return ""
    last = value.rfind("```")
    if last != -1:
        before = value.rfind("```", 0, last)
        if before != -1:
            return value[before + 3:last].strip()
    return value.strip()


def _clean_digest(text: str) -> str:
    """The core's digest of a text as it would be checked (so 'is in the list' means what 'duplicate' means)."""
    try:
        return _store_digest(check_text(text, "", screen=False)[0])
    except ValueError:
        return _store_digest(text.strip())


def _msg_text(content: dict) -> str:
    parts = content.get("parts")
    if isinstance(parts, list):
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    t = content.get("text")
    return t.strip() if isinstance(t, str) else ""


class _Found:
    """What the messages of an export said, in file order."""

    def __init__(self) -> None:
        self.lists: list[dict] = []          # {time, order, items}
        self.writes: list[dict] = []         # {time, order, text}
        self.instructions: list[dict] = []   # {time, order, about_you, how_to_respond}
        self.order = 0
        self.conversations = 0
        self.skipped: list[tuple[str, str]] = []     # (name, why)
        self.other_fields: dict[str, int] = {}
        self.broken_files: list[str] = []


def _scan_conversation(conv, idx: int, f: _Found) -> None:
    name = str(conv.get("id") or f"#{idx}") if isinstance(conv, dict) else f"#{idx}"
    if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
        f.skipped.append((name, "no usable mapping"))
        return
    f.conversations += 1
    conv_time = _num(conv.get("create_time"))
    if conv_time is None:
        conv_time = _num(conv.get("update_time"))
    lists, writes, instr, extra = [], [], [], {}
    for node in conv["mapping"].values():
        msg = node.get("message") if isinstance(node, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, dict):
            continue
        ctype = content.get("content_type")
        mtime = _num(msg.get("create_time"))
        t = mtime if mtime is not None else conv_time
        if ctype == "model_editable_context":
            ctx = content.get("model_set_context")
            if ctx is not None and not isinstance(ctx, str):
                f.skipped.append((name, "its memory list is not a string"))
                return
            if isinstance(ctx, str) and ctx.strip():
                lists.append({"time": t, "items": parse_memory_list(ctx)})
                for k, v in content.items():
                    if k not in ("content_type", "model_set_context") and isinstance(v, str) and v.strip():
                        extra[k] = extra.get(k, 0) + 1
        elif ctype == "user_editable_context":
            data = (msg.get("metadata") or {}).get("user_context_message_data") if isinstance(msg.get("metadata"), dict) else None
            data = data if isinstance(data, dict) else {}
            about = data.get("about_user_message")
            how = data.get("about_model_message")
            about = about.strip() if isinstance(about, str) and about.strip() else _fenced(content.get("user_profile"))
            how = how.strip() if isinstance(how, str) and how.strip() else _fenced(content.get("user_instructions"))
            if about or how:
                instr.append({"time": t, "about_you": about, "how_to_respond": how})
        elif ((msg.get("author") or {}).get("role") == "assistant" if isinstance(msg.get("author"), dict) else False) and \
                isinstance(msg.get("recipient"), str) and (msg["recipient"] == "bio" or msg["recipient"].startswith("bio.")):
            text = _msg_text(content)
            if text:
                writes.append({"time": mtime, "text": text})
    for rec in lists:                                       # committed only when the whole conversation was usable
        f.order += 1
        f.lists.append({**rec, "order": f.order})
    for rec in writes:
        f.order += 1
        f.writes.append({**rec, "order": f.order})
    for rec in instr:
        f.order += 1
        f.instructions.append({**rec, "order": f.order})
    for k, v in extra.items():
        f.other_fields[k] = f.other_fields.get(k, 0) + v


def _newest(records: list[dict]) -> dict | None:
    return max(records, key=lambda r: (r["time"] if r["time"] is not None else float("-inf"), r["order"]), default=None)


def _later(write: dict, newest: dict) -> bool:
    """A memory write is later than the newest list when its time is greater; without both times, file order decides."""
    if write["time"] is None or newest["time"] is None:
        return write["order"] > newest["order"]
    return write["time"] > newest["time"]


def _key(r: dict) -> tuple:
    return (r["time"] if r["time"] is not None else float("-inf"), r["order"])


def read_chatgpt(source: Path, view, include_removed: bool = False) -> ReadResult:
    res = ReadResult()
    res.groups = [("removed or older versions", "*-history/*"), ("memory", "memory#*"), ("memory writes", "memory-write/*"),
                  ("custom instructions", "custom-instructions/*")]
    f = _Found()
    with ExportSource(source) as src:
        files = conversation_files(src)
        for name in files:
            n_seen = 0
            try:
                with text_stream(src.open(name)) as fh:
                    for conv in iter_json_array(fh):
                        _scan_conversation(conv, n_seen, f)
                        n_seen += 1
            except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
                f.broken_files.append(f"{name} ({exc}; {n_seen} conversation(s) before that point were read)")
        _file_remarks(src, set(files), ("user.json",), res)
    if f.conversations or f.skipped:
        res.remarks.insert(0, f"{_plural(f.conversations, 'conversation')} {'was' if f.conversations == 1 else 'were'} looked "
                              f"through; their messages are not memory and were not imported (only the memory list, memory "
                              f"writes and custom instructions inside them are)")
    if f.other_fields:
        res.remarks.append("other fields of the memory list were not imported: "
                           + ", ".join(f"{k} ({_plural(n, 'time')})" for k, n in sorted(f.other_fields.items())))
    if f.skipped:
        why: dict[str, list[str]] = {}
        for name, reason in f.skipped:
            why.setdefault(reason, []).append(name)
        res.remarks.append(f"{_plural(len(f.skipped), 'conversation')} {'was' if len(f.skipped) == 1 else 'were'} skipped: "
                           + "; ".join(f"{len(v)} with {k}" if k == "no usable mapping" else f"{len(v)} whose {k.removeprefix('its ')}"
                                       for k, v in why.items())
                           + f" ({_names([n for n, _ in f.skipped])})")
    for broken in f.broken_files:
        res.remarks.append(f"conversation file {broken}")
    if not files:
        res.remarks.append("no conversation file (conversations.json, conversations-000.json ...) was found")
    entries: list[Entry] = []

    def add(text, origin, *, created=None, history: str = "", subject=None):
        body = text
        e = Entry(text=text_blocks.note_text(body, *subject) if subject else text, body=body, origin=origin, kind="preference",
                  created=created, history="" if include_removed else history)
        entries.append(e)

    newest = _newest(f.lists)
    if newest is None:
        if f.writes:
            res.remarks.append("this export has no memory list: nothing could be judged removed, so every memory write is "
                               "imported as an ordinary entry")
        newest_digests: set[str] = set()
        newest_day = "undated"
    else:
        newest_day = _day(newest["time"])
        newest_digests = {_clean_digest(t) for _, t in newest["items"]}
        for n, (date, text) in enumerate(newest["items"], 1):
            add(text, f"memory#{n}", created=date)
    per_day: dict[str, int] = {}
    for w in sorted(f.writes, key=lambda r: r["order"]):
        day = _day(w["time"])
        per_day[day] = per_day.get(day, 0) + 1
        origin = f"memory-write/{day}#{per_day[day]}"
        if newest is None or _later(w, newest) or _clean_digest(w["text"]) in newest_digests:
            add(w["text"], origin, created=w["time"])
        else:
            add(w["text"], origin, created=w["time"],
                history=f"a memory write from {day} that is not in ChatGPT's newest memory list ({newest_day}): "
                        f"removed or reworded in ChatGPT later")
    seen = set(newest_digests)
    for lst in sorted((x for x in f.lists if x is not newest), key=_key, reverse=True):
        day = _day(lst["time"])
        for n, (date, text) in enumerate(lst["items"], 1):
            d = _clean_digest(text)
            if d in seen:
                continue
            seen.add(d)
            add(text, f"memory-history/{day}#{n}", created=date,
                history=f"an item of an older memory list ({day}) that is not in ChatGPT's newest memory list ({newest_day}): "
                        f"removed or reworded in ChatGPT later")
    for field, label in (("about_you", "about you"), ("how_to_respond", "how to respond")):
        slug = label.replace(" ", "-")
        have = sorted((r for r in f.instructions if r[field]), key=_key, reverse=True)
        if not have:
            continue
        top, seen_text = have[0], {_clean_digest(have[0][field])}
        add(top[field], f"custom-instructions/{slug}", created=top["time"], subject=(SUBJECT, (label,)))
        for older in have[1:]:
            d = _clean_digest(older[field])
            if d in seen_text:
                continue
            seen_text.add(d)
            add(older[field], f"custom-instructions-history/{_day(older['time'])}/{slug}", created=older["time"],
                subject=(SUBJECT, (label,)),
                history=f"an older version of ChatGPT custom instructions ({label}, {_day(older['time'])}) that differs from the "
                        f"newest ({_day(top['time'])})")
    res.entries = entries
    return res


register(Reader("chatgpt-export", "the ZIP from ChatGPT's data export (or the folder it was unpacked into): the memory list, the "
                "memory writes and the custom instructions inside its conversation files; what you removed in ChatGPT stays "
                "removed unless you add --include-removed", detect_chatgpt, read_chatgpt, options=("include_removed",)))


# -- Claude -----------------------------------------------------------------------------------------------------------

def _base(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _memories_files(src: ExportSource) -> list[str]:
    return [n for n in src.names if n.count("/") <= 2 and _base(n) == "memories.json"]


def _claude_conversations(src: ExportSource) -> list[str]:
    return [n for n in src.names if n.count("/") <= 2 and _base(n) == "conversations.json"]


def _project_files(src: ExportSource) -> list[str]:
    """projects.json (a list) and projects/*.json (one project each), at most two folders deep."""
    return [n for n in src.names if n.count("/") <= 3 and (_base(n) == "projects.json" or
                                                           (n.endswith(".json") and n.rsplit("/", 2)[-2:-1] == ["projects"]))]


def detect_claude_export(source: Path) -> int:
    src = open_source(source)
    if src is None:
        return 0
    try:
        if _memories_files(src):
            return 4
        for name in _claude_conversations(src)[:3]:       # detection is the only place that looks into conversations.json
            try:
                if '"chat_messages"' in src.head(name):
                    return 4
            except (OSError, RuntimeError):
                continue
        return 0
    finally:
        src.close()


def _load_json(src: ExportSource, name: str, res: ReadResult):
    try:
        return json.loads(src.read_bytes(name).decode("utf-8-sig"))
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        res.remarks.append(f"{name} could not be read ({type(exc).__name__}: {exc}); nothing was taken from it")
        return None


def _blocks(entries: list[Entry], text: str, origin_fmt: str, label: str | None, *, kind: str | None) -> None:
    for n, b in enumerate(text_blocks.split_blocks(text), 1):
        entries.append(Entry(text=text_blocks.note_text(b.text, label, b.path), body=b.text, origin=origin_fmt.format(n=n), kind=kind))


def read_claude_export(source: Path, view) -> ReadResult:
    res = ReadResult()
    res.groups = [("memory", "memory#*"), ("project memory", "project-memory/*"),
                  ("project instructions", "project-instructions/*"), ("project descriptions", "project-description/*")]
    with ExportSource(source) as src:
        mem_files, proj_files, conv_files = _memories_files(src), _project_files(src), _claude_conversations(src)
        # projects: uuid -> record (a starter project is skipped, with a remark)
        projects: list[dict] = []
        starters: list[str] = []
        for name in proj_files:
            data = _load_json(src, name, res)
            for rec in (data if isinstance(data, list) else [data] if isinstance(data, dict) else []):
                if not isinstance(rec, dict):
                    continue
                if rec.get("is_starter_project") is True:
                    starters.append(str(rec.get("name") or rec.get("uuid") or "(unnamed)"))
                else:
                    projects.append(rec)
        names = [str(p.get("name") or "").strip() for p in projects]
        labels: dict[str, str] = {}
        for p, nm in zip(projects, names):
            uuid = str(p.get("uuid") or "")
            label = nm if nm and names.count(nm) == 1 else (uuid or nm or "project")
            labels[uuid] = label.replace("/", "-")
        # memory
        entries_mem: list[Entry] = []
        entries_pm: list[Entry] = []
        counters: dict[str, int] = {}
        n_mem, skipped_values = 0, 0
        for name in mem_files:
            data = _load_json(src, name, res)
            for rec in (data if isinstance(data, list) else [data] if isinstance(data, dict) else []):
                if not isinstance(rec, dict):
                    continue
                cm = rec.get("conversations_memory")
                if isinstance(cm, str):
                    for b in text_blocks.split_blocks(cm):
                        n_mem += 1
                        entries_mem.append(Entry(text=text_blocks.note_text(b.text, None, b.path), body=b.text,
                                                 origin=f"memory#{n_mem}", kind="preference"))
                elif cm not in (None, ""):
                    skipped_values += 1
                pm = rec.get("project_memories")
                for uuid, text in (pm.items() if isinstance(pm, dict) else []):
                    if not isinstance(text, str):
                        skipped_values += 1
                        continue
                    label = labels.get(str(uuid), str(uuid).replace("/", "-"))
                    for b in text_blocks.split_blocks(text):
                        counters[label] = counters.get(label, 0) + 1
                        entries_pm.append(Entry(text=text_blocks.note_text(b.text, label, b.path), body=b.text,
                                                origin=f"project-memory/{label}#{counters[label]}"))
        instr: list[Entry] = []
        desc: list[Entry] = []
        docs: list[tuple[str, int]] = []
        for p in projects:
            label = labels[str(p.get("uuid") or "")]
            pt = p.get("prompt_template")
            if isinstance(pt, str) and pt.strip():
                _blocks(instr, pt, f"project-instructions/{label}#{{n}}", label, kind=None)
            d = p.get("description")
            if isinstance(d, str) and d.strip():
                desc.append(Entry(text=text_blocks.note_text(d.strip(), label, ()), body=d.strip(),
                                  origin=f"project-description/{label}"))
            for doc in p.get("docs") if isinstance(p.get("docs"), list) else []:
                if isinstance(doc, dict):
                    docs.append((str(doc.get("filename") or "(unnamed)"), len(str(doc.get("content") or "").encode("utf-8"))))
        res.entries = entries_mem + entries_pm + instr + desc
        if starters:
            res.remarks.append(f"{_plural(len(starters), 'starter project')} {'was' if len(starters) == 1 else 'were'} skipped: "
                               f"Claude creates it for every account ({_names(starters)})")
        if docs:
            total = sum(s for _, s in docs)
            res.remarks.append(f"{_plural(len(docs), 'project document')} {'was' if len(docs) == 1 else 'were'} not imported: "
                               f"documents are files, not memories ({total:,} bytes in all; {_names([n for n, _ in docs])})")
        if skipped_values:
            res.remarks.append(f"{_plural(skipped_values, 'memory value')} in memories.json {'was' if skipped_values == 1 else 'were'} "
                               f"not text and was not imported")
        if not mem_files and not proj_files:
            res.remarks.append("no memories.json and no projects file was found in this export")
        for name in conv_files:
            res.remarks.append(f"{name} ({src.sizes.get(name, 0) / 1e6:.1f} MB) was not read: conversations are not memory")
        _file_remarks(src, set(mem_files) | set(proj_files) | set(conv_files), ("users.json",), res)
    return res


register(Reader("claude-export", "the ZIP from Claude's data export (or the folder it was unpacked into): memories.json (what Claude "
                "remembers about you and about each project) and each project's instructions and description; conversations "
                "and documents are not imported", detect_claude_export, read_claude_export))
