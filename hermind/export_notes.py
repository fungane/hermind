"""`hermind export`: hand over everything in the database, in a format the receiving tool can read.

One record per row of `notes` (every status, every column, plus the note's entities and outgoing links) is the full
export; the other formats are views of the same records. Export only reads: the database is opened the way
`hermind search` opens it and nothing is written to it. Not exported: vectors, the search index, tombstones (digests
of forgotten text) and the usage log. Copying the database file (tools/backup.py) is the way to move those too.

Standard library only; lives in the repository root so the wheel ships it.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

try:
    from . import md_export
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import md_export

FORMATS = ("json", "jsonl", "markdown")
FIRST_KEYS = ("id", "kind", "content", "keywords", "created_at")    # the five keys of the first export, kept first
STATUS_ORDER = ("active", "merged", "superseded", "rejected")


class ExportError(Exception):
    """A problem the user can fix; the message is one line."""


# -- reading the records -------------------------------------------------------------------------------------------------


def collect_records(conn, *, live_only: bool = False, scope: str | None = None) -> list[dict]:
    """The full record of every note, ordered by id: every column of `notes` under its own name (the five keys of the
    old export first), then `entities` ([{"name", "aliases"}]) and `links` ([{"to", "rel"}], outgoing, from
    note_links). *live_only* keeps notes with status 'active'; *scope* keeps one scope. Only reads."""
    sql, args = "SELECT * FROM notes", []
    where = []
    if live_only:
        where.append("status = 'active'")
    if scope and scope != "*":
        where.append("scope = ?")
        args.append(scope)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    records: list[dict] = []
    for r in rows:
        cols = list(r.keys())
        values = dict(zip(cols, tuple(r)))
        rec = {k: values[k] for k in FIRST_KEYS if k in values}
        rec.update((k, v) for k, v in values.items() if k not in rec)
        records.append(rec)
    ents: dict[int, list] = {}
    for nid, name, aliases in conn.execute(
            "SELECT ne.note_id, e.name, e.aliases FROM note_entities ne JOIN entities e ON e.id = ne.entity_id "
            "ORDER BY ne.note_id, e.name, e.id"):
        ents.setdefault(nid, []).append({"name": name, "aliases": [a for a in (aliases or "").split("|") if a]})
    links: dict[int, list] = {}
    for src, dst, rel in conn.execute("SELECT src, dst, rel FROM note_links ORDER BY src, dst, rel"):
        links.setdefault(src, []).append({"to": dst, "rel": rel})
    for rec in records:
        rec["entities"] = ents.get(rec["id"], [])
        rec["links"] = links.get(rec["id"], [])
    return records


def count_by_status(records: list[dict]) -> dict[str, int]:
    """Notes per status, the usual statuses first and any other status after them."""
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    order = [s for s in STATUS_ORDER if s in counts] + sorted(s for s in counts if s not in STATUS_ORDER)
    return {s: counts[s] for s in order}


def status_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{s} {n}" for s, n in counts.items()) or "no notes"


# -- writing -------------------------------------------------------------------------------------------------------------


def to_json(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2)


def write_private_file(path: str, text: str, *, force: bool = False) -> Path:
    """Write *text* to *path* with mode 600. An existing file is never overwritten unless *force*."""
    dest = Path(os.path.expanduser(path))
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(dest, flags, 0o600)
    except FileExistsError:
        raise ExportError(f"{dest} already exists: choose another file name or pass --force to overwrite it") from None
    with contextlib.ExitStack() as stack:
        try:
            os.fchmod(fd, 0o600)                        # a file that --force overwrites may have had another mode
        except (AttributeError, OSError):
            pass                                        # Windows has no POSIX modes
        f = stack.enter_context(os.fdopen(fd, "w", encoding="utf-8"))
        f.write(text if text.endswith("\n") else text + "\n")
    return dest


# -- entity and observation lines (--format jsonl) ----------------------------------------------------------------------


NOTE_NAME = "hermind-note-{}"
HISTORY_STATUSES = ("superseded", "merged")          # the older versions --include-history adds (rejected notes never go out)
_VERSION_LINKS = ("version_of", "superseded_by", "merged_into")


def note_name(note_id: int) -> str:
    """The stable name a note has in the entity lines: the same note always gets the same name."""
    return NOTE_NAME.format(note_id)


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def markers(rec: dict, *, now: float) -> list[str]:
    """What a note says about itself, as bracketed statements, in this order and only when each one holds. Many receiving
    tools keep only the text, so the note has to say in its text that it is pending, outdated, expired or an older version."""
    out = []
    if rec["status"] == "active" and rec.get("review") == "pending":
        out.append("(waiting for review)")
    newer = sorted(l["to"] for l in rec["links"] if l["rel"] == "stale_by")
    if newer:
        out.append(f"(outdated: {note_name(newer[0])} is newer)")
    if rec.get("valid_to") is not None and rec["valid_to"] < now:
        out.append(f"(no longer valid after {_day(rec['valid_to'])})")
    if rec["status"] in HISTORY_STATUSES:
        target = rec.get("canonical_id")
        if target is None:
            target = next((l["to"] for rel in _VERSION_LINKS for l in rec["links"] if l["rel"] == rel), None)
        out.append(f"(an older version of {note_name(target)}, kept for history)" if target is not None
                   else "(an older version, kept for history)")
    return out


def _line(obj: dict) -> str:
    """One JSON object on one line. U+2028, U+2029 and U+0085 are escaped too: a reader that splits lines the way
    str.splitlines() does would otherwise cut a note in two."""
    text = json.dumps(obj, ensure_ascii=False)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\u0085")


def jsonl_lines(records: list[dict], *, include_history: bool = False, now: float | None = None) -> list[str]:
    """One entity line per exported note (status active, pending ones included; with *include_history* the older
    versions too), then one relation line per link whose two ends are both exported."""
    now = time.time() if now is None else now
    keep = ("active",) + (HISTORY_STATUSES if include_history else ())
    chosen = [r for r in records if r["status"] in keep]
    lines = []
    for rec in chosen:
        text = " ".join(markers(rec, now=now) + [rec["content"]])
        lines.append(_line({"type": "entity", "name": note_name(rec["id"]), "entityType": rec["kind"],
                            "observations": [text], "hermind": rec}))
    ids = {r["id"] for r in chosen}
    for rec in chosen:
        for l in rec["links"]:
            if l["to"] in ids:
                lines.append(_line({"type": "relation", "from": note_name(rec["id"]), "to": note_name(l["to"]),
                                    "relationType": l["rel"]}))
    return lines


def run_export(store, *, fmt: str = "json", out: str | None = None, force: bool = False, live_only: bool = False,
               include_history: bool = False, by: str = "kind", scope: str | None = None, now: float | None = None) -> dict:
    """Export from an open database (a DecisionStore; only read). With *out* the text goes to that file (for markdown,
    that folder) and the result says where; without it the text is returned in result["text"] for the caller to print."""
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r}: choose one of {', '.join(FORMATS)}")
    if live_only and fmt != "json":
        raise ExportError("--live-only is for --format json (jsonl and markdown already leave out older versions "
                          "unless you add --include-history)")
    if include_history and fmt == "json":
        raise ExportError("--include-history is for --format jsonl and markdown (the JSON export always has every note)")
    if fmt == "markdown":
        if not out:
            raise ExportError("--format markdown writes a folder: give it with --out DIR")
        try:
            res = md_export.write_folder(store, out, by=by, include_history=include_history, scope=scope, force=force)
        except md_export.FolderNotEmpty as e:
            raise ExportError(str(e)) from None
        return {"ok": True, "format": fmt, "exported": res["notes"], "folders": res["folders"], "file": res["root"],
                "by_status": count_by_status([{"status": k} for k, n in res["by_status"].items() for _ in range(n)])}
    records = collect_records(store.conn, live_only=live_only, scope=scope)
    if fmt == "jsonl":
        lines = jsonl_lines(records, include_history=include_history, now=now)
        shown = [r for r in records if r["status"] in (("active",) + (HISTORY_STATUSES if include_history else ()))]
        counts = count_by_status(shown)
        result = {"ok": True, "format": fmt, "exported": len(shown), "relations": len(lines) - len(shown),
                  "by_status": counts}
        text = "\n".join(lines)
    else:
        counts = count_by_status(records)
        result = {"ok": True, "format": fmt, "exported": len(records), "by_status": counts}
        text = to_json(records)
    if out:
        result["file"] = str(write_private_file(out, text, force=force))
    else:
        result["text"] = text
    return result
