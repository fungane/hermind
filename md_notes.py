"""Shared helpers for the Markdown export, tools/import_md.py and `hermind import`: the one-file-per-note Markdown format.

A note file is YAML front matter (a small subset written and read here, no PyYAML needed) followed by the note text:

    ---
    id: 123
    kind: decision
    status: active              # active | pending (| superseded | merged with --include-hidden)
    tags: ["hermes", "backup"]
    trust: 0.5
    created: 2026-10-01
    valid_from: 2026-10-01      # empty = since it was created
    valid_to:                   # empty = still valid
    supersedes: [12]            # older notes this one replaced (read-only)
    outdated_by: 15             # the newer note that replaced / contradicts this one (read-only)
    pinned: false               # read-only
    scope: default              # read-only
    keywords: "alias one alias two"
    rev: 3f9c1a07b2d4           # fingerprint of the exported text + editable fields; lets import detect conflicts
    ---
    The note text, free Markdown.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from .store_common import parse_valid
except ImportError:  # loaded as a top-level module (tools/, tests)
    from store_common import parse_valid

EDITABLE = ("kind", "tags", "trust", "valid_from", "valid_to")      # applied on import besides text and keywords
ORDER = ("id", "kind", "status", "tags", "trust", "created", "valid_from", "valid_to", "supersedes", "outdated_by",
         "pinned", "scope", "keywords", "rev")
_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def iso_day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts is not None else ""


def fmt(value) -> str:
    if value is None or value == "" or value == []:
        return "[]" if value == [] else ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(round(value, 3)) if isinstance(value, float) else str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) if isinstance(v, str) else fmt(v) for v in value) + "]"
    s = str(value)
    return s if (_SAFE.match(s) or _DATE.match(s)) else json.dumps(s, ensure_ascii=False)


def parse_value(raw: str):
    raw = raw.strip()
    if raw == "" or raw.lower() in ("null", "~"):
        return None
    if raw[0] in '"[':
        try:     # a JSON string / list; anything after it (a trailing "# comment") is ignored
            return json.JSONDecoder().raw_decode(raw)[0]
        except ValueError:
            pass
        if raw[0] == '"':
            return raw.strip('"')
        close = raw.find("]")
        if close != -1:    # [alpha, beta]   # comment
            return [x.strip().strip("\"'") for x in raw[1:close].split(",") if x.strip()]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d*|-?\.\d+", raw):
        return float(raw)
    return raw.strip("'")


def parse_note_file(text: str) -> tuple[dict, str]:
    """(front matter dict, body). A file without front matter is all body."""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---", 3)
    while end != -1 and text[end + 4:end + 5] not in ("\n", ""):
        end = text.find("\n---", end + 1)
    if end == -1:
        return {}, text.strip()
    meta: dict = {}
    for line in text[4:end].split("\n"):
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        value = rest if rest.lstrip().startswith(('"', "[")) else re.sub(r"\s+#.*$", "", rest)   # plain values: strip "# comment"
        meta[key.strip()] = parse_value(value)
    return meta, text[end + 4:].strip()


def parse_front(text: str) -> tuple[dict, dict, str]:
    """(top-level front matter, keys nested under a parent key, body) for agent files (Claude Code memory topics, rules,
    instruction files). Unlike parse_note_file it keeps indented keys apart (``metadata:`` with keys under it) and reads
    block lists (``paths:`` followed by ``- item`` lines). A file without front matter is all body."""
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return {}, {}, text.strip()
    end = text.find("\n---", 3)
    while end != -1 and text[end + 4:end + 5] not in ("\n", ""):
        end = text.find("\n---", end + 1)
    if end == -1:
        return {}, {}, text.strip()
    top: dict = {}
    nested: dict = {}
    parent = False                     # the last top-level key had no value: indented keys below it are nested
    last: tuple[dict, str] | None = None
    for line in text[4:end].split("\n"):
        line = line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "-" or stripped.startswith("- "):             # an item of a block list under the last empty key
            if last is not None and (last[0].get(last[1]) is None or isinstance(last[0].get(last[1]), list)):
                cur = last[0].get(last[1]) or []
                cur.append(parse_value(stripped[1:]))
                last[0][last[1]] = cur
            continue
        if ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        value = rest if rest.lstrip().startswith(('"', "[")) else re.sub(r"\s+#.*$", "", rest)
        indented = line[:1] in (" ", "\t")
        if indented and parent:
            nested[key] = parse_value(value)
            last = (nested, key)
        else:
            top[key] = parse_value(value)
            parent = not value.strip()
            last = (top, key)
    return top, nested, text[end + 4:].strip()


def as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def tag_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return sorted({str(t).strip().lower() for t in as_list(value) if str(t).strip()})


def fields(content: str, keywords: str, kind: str, tags, trust, valid_from, valid_to, pending: bool) -> dict:
    """The editable state of a note in comparable form (dates as ISO days, tags sorted, trust rounded)."""
    return {"content": (content or "").strip(), "keywords": " ".join((keywords or "").split()), "kind": kind,
            "tags": tag_list(tags), "trust": round(float(trust if trust is not None else 0.5), 3),
            "valid_from": valid_from or "", "valid_to": valid_to or "", "pending": bool(pending)}


def rev_of(f: dict) -> str:
    return hashlib.sha1(json.dumps(f, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def note_fields(note: dict) -> dict:
    """fields() of a store note dict."""
    return fields(note["content"], note.get("keywords", ""), note["kind"], note.get("tags", ""), note.get("trust"),
                  iso_day(note.get("valid_from")), iso_day(note.get("valid_to")), note.get("review") == "pending")


def file_fields(meta: dict, body: str) -> dict:
    """fields() of a parsed note file; missing front matter values fall back to neutral defaults."""
    status = str(meta.get("status") or "").lower()
    return fields(body, meta.get("keywords") or "", str(meta.get("kind") or "").lower(), meta.get("tags"),
                  meta.get("trust"), str(meta.get("valid_from") or ""), str(meta.get("valid_to") or ""),
                  status == "pending")


def note_status(note: dict) -> str:
    if note["status"] == "active":
        return "pending" if note.get("review") == "pending" else "active"
    return note["status"]


def link_meta(conn, note: dict) -> dict:
    """status / supersedes / outdated_by of a note, from the note_links table (see render)."""
    nid = note["id"]
    sup = sorted(r[0] for r in conn.execute("SELECT src FROM note_links WHERE dst=? AND rel='superseded_by'", (nid,)))
    stale = conn.execute("SELECT dst FROM note_links WHERE src=? AND rel='stale_by'", (nid,)).fetchone()
    outdated = stale[0] if stale else (note.get("canonical_id") if note["status"] == "superseded" else None)
    return {"status": note_status(note), "supersedes": sup, "outdated_by": outdated}


_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def slug(text: str, limit: int = 40) -> str:
    s = _BAD.sub(" ", text or "")
    s = re.sub(r"\s+", "-", s.strip()).strip("-.")
    return s[:limit].strip("-.") or "note"


def render(note: dict, meta: dict) -> str:
    """The text of a note file; *meta* holds the computed values (status, supersedes, outdated_by, created, ...)."""
    f = note_fields(note)
    values = {"id": note["id"], "kind": note["kind"], "status": meta["status"], "tags": tag_list(note.get("tags", "")),
              "trust": f["trust"], "created": iso_day(note["created_at"]), "valid_from": f["valid_from"],
              "valid_to": f["valid_to"], "supersedes": meta.get("supersedes", []), "outdated_by": meta.get("outdated_by"),
              "pinned": bool(note.get("pinned")), "scope": note.get("scope", "default"), "keywords": f["keywords"],
              "rev": rev_of(f)}
    head = "\n".join(f"{k}: {fmt(values[k])}".rstrip() for k in ORDER)
    return f"---\n{head}\n---\n{note['content'].strip()}\n"


# -- reading a folder of note files (shared by tools/import_md.py and `hermind import`) -----------------------------------


TEXT_SUFFIXES = (".md", ".markdown", ".mdx", ".txt")      # the files of a folder that are read as notes
_INDEX_ITEM = re.compile(r"^- \[(?:#\d+|[^\]]+)\]\([^)]*\)( .*)?$")
_INDEX_COUNT = re.compile(r"^\d+ notes?$")


def is_export_index(text: str) -> bool:
    """True when *text* is an index.md that the Markdown export wrote: a ``# heading``, a ``N notes`` line (or, for the
    top index, ``N notes from DB, layout: by KIND``), and then only ``- [#id](file.md) ...`` / ``- [dir](dir/index.md)
    ...`` lines. Anything else, even a file called index.md that looks a little like it, is an ordinary note."""
    lines = [ln.rstrip() for ln in (text or "").lstrip("\ufeff").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 3 or not lines[0].startswith("# ") or lines[1] != "":
        return False
    count = lines[2]
    if lines[0] == "# hermind export":
        if not re.match(r"^\d+ notes from .+, layout: by (kind|entity)$", count):
            return False
    elif not _INDEX_COUNT.match(count):
        return False
    rest = lines[3:]
    if rest and rest[0] != "":
        return False
    return all(not ln or _INDEX_ITEM.match(ln) for ln in rest)


class Walk:
    """What a folder holds: the text notes (sorted), the other files counted per extension, and the files inside hidden
    folders counted per folder name. Hermind's own index.md files are not notes (see is_export_index)."""

    def __init__(self) -> None:
        self.notes: list[Path] = []
        self.other: dict[str, int] = {}
        self.hidden: dict[str, int] = {}
        self.indexes = 0                     # index.md files that the Markdown export wrote (passed by)

    def remark(self) -> str | None:
        """One sentence that accounts for everything that is not a note, or None when there is nothing to say."""
        parts = []
        if self.other:
            n = sum(self.other.values())
            parts.append(f"{n} file(s) that are not text notes were not read ("
                         + ", ".join(f"{c} {e}" for e, c in sorted(self.other.items(), key=lambda kv: (-kv[1], kv[0]))) + ")")
        if self.hidden:
            n = sum(self.hidden.values())
            parts.append(f"{n} file(s) inside hidden folders were not read ("
                         + ", ".join(f"{c} in {d}/" for d, c in sorted(self.hidden.items(), key=lambda kv: (-kv[1], kv[0]))) + ")")
        if self.indexes:
            parts.append(f"{self.indexes} index.md file(s) written by Hermind's exporter were passed by: they list notes, "
                         f"they are not notes")
        return "; ".join(parts) or None


def walk_folder(root: Path) -> Walk:
    """THE function that decides which files of a folder are notes (tools/import_md.py and `hermind import` both call
    it): .md, .markdown, .mdx and .txt files, except an index.md that Hermind's exporter wrote and anything inside a
    hidden (dot) folder. Everything else is counted, never silently dropped."""
    w = Walk()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        hidden_dirs = sorted(d for d in dirnames if d.startswith("."))
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for d in hidden_dirs:
            n = sum(len(fs) for _, _, fs in os.walk(here / d))
            if n:
                w.hidden[d] = w.hidden.get(d, 0) + n
        for name in sorted(filenames):
            path = here / name
            if path.suffix.lower() in TEXT_SUFFIXES:
                if name == "index.md" and _is_export_index_file(path):
                    w.indexes += 1
                else:
                    w.notes.append(path)
            else:
                ext = path.suffix.lower() or "(no extension)"
                w.other[ext] = w.other.get(ext, 0) + 1
    w.notes.sort()
    return w


def _is_export_index_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(2_000_000)
        return is_export_index(head.decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return False


def find_files(root: Path) -> list[Path]:
    """The text notes under *root* (see walk_folder)."""
    return walk_folder(root).notes


def decode_text(data: bytes, encoding: str | None = None) -> tuple[str | None, str]:
    """(text, "") or (None, reason). UTF-8 with or without a byte-order mark, UTF-16 and UTF-32 with one; any other
    file needs *encoding* (a codec name such as big5, cp1252). Text with NUL characters is refused: it is binary data
    or UTF-16 without a byte-order mark."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        enc = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        enc = "utf-16"
    else:
        enc = "utf-8-sig"
    try:
        text = data.decode(enc)
    except UnicodeDecodeError as exc:
        if enc != "utf-8-sig":
            return None, f"not valid {enc.upper()} text ({exc.reason})"
        if not encoding:
            return None, ("not valid UTF-8 text; if you know its encoding say so with --encoding NAME "
                          "(for example big5, gb18030, cp1252, shift_jis), or save it as UTF-8")
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc2:
            return None, f"cannot be decoded as {encoding}: {exc2.reason} at byte {exc2.start}"
        except LookupError:
            return None, f"unknown encoding {encoding!r}"
    if "\x00" in text:
        return None, ("contains NUL characters: it is binary data, or UTF-16 text without a byte-order mark; "
                      "save it as UTF-8 (or UTF-16 with a byte-order mark)")
    return text, ""


# -- the subject of a note file: what the title / name / file name adds to a body that lacks it ------------------------

GENERIC_NAMES = {"untitled", "note", "new note", "index", "readme"}
_WORDLESS_DATETIME = re.compile(r"^\d{8}t\d{2,6}z?$", re.I)
_WORDLESS_HEX = re.compile(r"^[0-9a-f]*\d[0-9a-f]*$", re.I)       # hex that contains a digit: a8f3c2d1, 3f9c, 2026


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower())


def has_words(name: str) -> bool:
    """False for a name that carries no words: only digits, dates, times, hex or UUID-like identifiers."""
    tokens = [t for t in re.split(r"[\s_\-.:]+", name) if t]
    return any(not (_WORDLESS_HEX.match(t) or _WORDLESS_DATETIME.match(t)) for t in tokens)


def spaced(name: str) -> str:
    """A slug-like name with its dashes and underscores between words turned into spaces."""
    return " ".join(re.sub(r"[-_]+", " ", name).split())


def subject_of(meta: dict, file_stem: str, *, use_file_name: bool = True) -> str:
    """The subject of a note file: front matter ``title``, else ``name``, else the file name without its extension
    (``""`` when there is none worth adding). Hermind's own export files (they carry id and rev), generic file names
    (untitled, note, new note, index, readme) and names without words add nothing; a leading ``00012-`` is not part
    of the subject."""
    for key in ("title", "name"):
        v = meta.get(key)
        if v not in (None, "") and not isinstance(v, (list, dict)) and str(v).strip():
            v = str(v).strip()
            return spaced(v) if key == "name" and not re.search(r"\s", v) and re.search(r"[-_]", v) else v
    if not use_file_name or (meta.get("id") is not None and meta.get("rev")):
        return ""
    stem = re.sub(r"^\d+-", "", file_stem).strip()
    name = spaced(stem)
    if not name or name.lower() in GENERIC_NAMES or not has_words(stem):
        return ""
    return name


def describe(meta: dict, body: str, file_stem: str, *, use_file_name: bool = True, label: str | None = None) -> str:
    """The note text: the body, with the subject (and the description) in front when the body does not already say
    them. Format: subject, then description, a blank line, the body. A piece is added only when, with both lower-cased
    and stripped of punctuation and spacing, it is not a substring of the body. *label* (the project or agent a note
    belongs to) goes in front of the subject on the first line, under the same rule."""
    flat = _squash(body)
    pieces = []
    subject = subject_of(meta, file_stem, use_file_name=use_file_name)
    first = [x for x in (label, subject) if x and _squash(x) and _squash(x) not in flat]
    if len(first) == 2 and _squash(first[0]) == _squash(first[1]):
        first = first[:1]
    if first:
        pieces.append(" › ".join(first))
    for key in ("description", "summary"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            d = " ".join(v.split())
            if _squash(d) and _squash(d) not in flat:
                pieces.append(d)
            break
    return "\n".join(pieces) + "\n\n" + body if pieces else body


def day10(value) -> str:
    """A front matter date as an ISO day ('' for none); ValueError for a date that cannot be read."""
    v = str(value or "").strip()
    if not v:
        return ""
    return iso_day(parse_valid(v))


def int_id(meta: dict) -> int | None:
    """The `id` of a note file when it is a whole number, else None."""
    nid = meta.get("id")
    return nid if isinstance(nid, int) and not isinstance(nid, bool) else None


def owner_of(meta: dict, versions: list[dict]) -> dict | None:
    """The note this file belongs to, or None.

    A file belongs to note #id of *this* database only when it carries both `id` and `rev` and that `rev` is the
    fingerprint of the note's current version or of one of its earlier versions (*versions*: the current note first,
    then its history rows, as dicts of columns). Anything else (no such note, a `rev` that never belonged to it, no
    `rev`) means the `id` is someone else's numbering and the file is an ordinary entry. Earlier versions are known
    for text and keyword edits; a metadata-only edit (tags, kind, trust, dates) leaves no history row, so an export
    made before such an edit is not recognised."""
    if int_id(meta) is None or not meta.get("rev") or not versions or versions[0]["id"] != meta["id"]:
        return None
    revs = {rev_of(note_fields(versions[0]))}
    try:
        file_end = day10(meta.get("valid_to"))
    except ValueError:
        file_end = ""
    for old in versions[1:]:
        f = note_fields(old)
        # update() stamps a history row's valid_to with the time of the edit, so the end date the version had when it
        # was exported is gone: try none, and the one the file itself states (the rest of the fingerprint still has to match)
        revs |= {rev_of({**f, "valid_to": end}) for end in {f["valid_to"], "", file_end}}
    return versions[0] if str(meta["rev"]) in revs else None


def compare_file(note: dict, meta: dict, body: str) -> tuple[dict, dict, list[str]]:
    """(database fields, file fields, names of the fields that differ). Keys missing from the file keep the database
    value (they are not an instruction to clear it) and only `status: active` on a pending note is an instruction.
    *meta* must already carry normalised dates (see day10)."""
    db_f = note_fields(note)
    f = file_fields(meta, body)
    for key in ("keywords", "kind", "tags", "trust", "valid_from", "valid_to"):
        if key not in meta:
            f[key] = db_f[key]
    if str(meta.get("status") or "").lower() != "active":
        f["pending"] = db_f["pending"]
    return db_f, f, [k for k in db_f if f[k] != db_f[k]]
