"""The ``hermind-json`` reader: Hermind's own JSON export (``hermind export``) comes back in, old versions included.

A live note (status ``active``) becomes an entry with every field the export carries that a note can be written with.
An older version (``superseded`` or ``merged``) is stored as an older version of the new note it belongs to, or, when
that note is not new, listed as history with the reason. Rejected notes are not imported. The links between two notes
this import writes are recreated, and the entity aliases of the file are added to the entities of the new notes.
The first export of Hermind (five keys, live notes only) is read too, with ``--from hermind-json``.

Standard library only; lives in the repository root so the wheel ships it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:
    from .import_core import Entry, ImportFailed, Reader, ReadResult, register
    from .store_common import IMPORT_HERMIND
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    from import_core import Entry, ImportFailed, Reader, ReadResult, register
    from store_common import IMPORT_HERMIND

HEAD = 64 * 1024                                   # detection looks at no more than this
NEED = ("id", "content", "kind", "status")         # the keys of the first object that make a file Hermind's export
OLDER = ("superseded", "merged")
VERSION_LINKS = ("version_of", "superseded_by", "merged_into")
DEFAULT_REL = {"superseded": "version_of", "merged": "merged_into"}
AFTER_KEYS = ("event_at", "proof_count", "gen_keywords", "gen_at", "gen_model", "learned_terms")
GROUPS = [("notes", "note/*"), ("older versions", "version/*"), ("rejected", "rejected/*")]


def detect_hermind_json(source: Path) -> int:
    """4 for a ``.json`` file whose first 64 KiB start a JSON array of objects with id, content, kind and status."""
    if not source.is_file() or source.suffix.lower() != ".json":
        return 0
    try:
        with open(source, "rb") as f:
            head = f.read(HEAD).decode("utf-8-sig", errors="ignore").lstrip()
    except OSError:
        return 0
    if not head.startswith("["):
        return 0
    rest = head[1:].lstrip()
    if not rest.startswith("{"):
        return 0
    try:
        first, _ = json.JSONDecoder().raw_decode(rest)
    except ValueError:                              # the first note goes on past what was read: the keys seen so far must do
        return 4 if all(re.search(rf'"{k}"\s*:', rest) for k in NEED) else 0
    return 4 if isinstance(first, dict) and all(k in first for k in NEED) else 0


def _number(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _date(v):
    """A date as the export writes it (epoch seconds); a text date is passed on for the core to read or to drop."""
    return _number(v) if _number(v) is not None else (v if isinstance(v, str) and v.strip() else None)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _tags(v) -> list[str]:
    if isinstance(v, str):
        return v.replace(",", " ").split()
    return [str(t) for t in v if str(t).strip()] if isinstance(v, list) else []


def _entities(v) -> tuple | None:
    """The entities the record gives the note, exactly; None when the record has no usable list (the first export)."""
    if not isinstance(v, list):
        return None
    out = []
    for e in v:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip():
            als = e.get("aliases")
            out.append((e["name"], tuple(a for a in als if isinstance(a, str) and a) if isinstance(als, list) else ()))
    return tuple(out)


def _links(v) -> tuple:
    return tuple((l["to"], l["rel"]) for l in (v if isinstance(v, list) else [])
                 if isinstance(l, dict) and _int(l.get("to")) is not None and isinstance(l.get("rel"), str) and l["rel"])


def _after(rec: dict) -> dict | None:
    out = {}
    for k in AFTER_KEYS:
        if k not in rec:
            continue
        v = rec[k]
        if k in ("event_at", "gen_at") and (v is None or _number(v) is not None):
            out[k] = v
        elif k == "proof_count" and _int(v) is not None and v >= 1:
            out[k] = v
        elif k in ("gen_keywords", "gen_model", "learned_terms") and isinstance(v, str):
            out[k] = v
    return out or None


def _target(rec: dict) -> tuple[int | None, str]:
    """The note an older version belongs to (its canonical_id, else the dst of its version_of / superseded_by /
    merged_into link) and the rel of its link to it."""
    links = _links(rec.get("links"))
    cid = _int(rec.get("canonical_id"))
    if cid is None:
        cid, rel = next(((to, rel) for to, rel in links if rel in VERSION_LINKS), (None, ""))
        return cid, rel
    rel = next((rel for to, rel in links if to == cid and rel in VERSION_LINKS), "")
    return cid, rel or DEFAULT_REL.get(rec.get("status"), "version_of")


def read_hermind_json(source: Path, view) -> ReadResult:
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ImportFailed(f"{source.name} is not a UTF-8 text file ({exc.reason}); expected the output of `hermind export`") from None
    except ValueError as exc:
        raise ImportFailed(f"{source.name} is not valid JSON ({exc}); expected the output of `hermind export` (a JSON array "
                           f"of notes; a --format jsonl or markdown export is not read by this reader)") from None
    if not isinstance(data, list):
        raise ImportFailed(f"{source.name} is not a JSON array of notes: expected the output of `hermind export`")
    res = ReadResult(groups=list(GROUPS))
    seen_ids: set[int] = set()
    counts = {"live": 0, "older": 0, "rejected": 0, "other": 0}
    skipped = repeated = 0
    no_status = False
    for pos, rec in enumerate(data, 1):
        if not isinstance(rec, dict) or not isinstance(rec.get("content"), str):
            skipped += 1
            continue
        nid = _int(rec.get("id"))
        if nid is not None and nid in seen_ids:
            repeated += 1
            nid_for_links = None
        else:
            nid_for_links = nid
            if nid is not None:
                seen_ids.add(nid)
        label = nid if nid is not None else f"#{pos}"
        status = rec.get("status")
        if status is None:
            no_status, status = True, "active"
        common = dict(text=rec["content"], kind=rec.get("kind") if isinstance(rec.get("kind"), str) else None,
                      keywords=rec["keywords"] if isinstance(rec.get("keywords"), str) else "", created=_date(rec.get("created_at")),
                      tags=_tags(rec.get("tags")), pinned=bool(rec.get("pinned")), source_session=IMPORT_HERMIND,
                      valid_from=_date(rec.get("valid_from")), valid_to=_date(rec.get("valid_to")),
                      pending=rec.get("review") == "pending", trust=rec.get("trust"),
                      scope=rec["scope"] if isinstance(rec.get("scope"), str) and rec["scope"].strip() else None,
                      source_id=nid_for_links, after=_after(rec), entities=_entities(rec.get("entities")),
                      links=_links(rec.get("links")))
        if status == "active":
            counts["live"] += 1
            res.entries.append(Entry(origin=f"note/{label}", **common))
        elif status in OLDER:
            counts["older"] += 1
            target, rel = _target(rec)
            if target is None:
                res.entries.append(Entry(origin=f"version/{label}", status=status, history=(
                    f"an older version ({status}) with no note it belongs to: the record has no canonical_id and no "
                    f"version_of, superseded_by or merged_into link"), **common))
            else:
                res.entries.append(Entry(origin=f"version/{label}", status=status, version_of=target, version_rel=rel, **common))
        elif status == "rejected":
            counts["rejected"] += 1
            res.entries.append(Entry(origin=f"rejected/{label}", history="rejected in review", **common))
        else:
            counts["other"] += 1
            res.entries.append(Entry(origin=f"other/{label}", history=f"status {status!r} is not a live note; not imported",
                                     **common))
    parts = [f"{counts['live']} live"] + [f"{n} {w}" for n, w in ((counts["older"], "older version(s)"),
                                                                    (counts["rejected"], "rejected (never imported)"),
                                                                    (counts["other"], "with another status (never imported)")) if n]
    res.remarks.append(f"{source.name}: {len(res.entries)} note record(s): " + ", ".join(parts))
    if no_status:
        res.remarks.append("the records have no status field: this is the first export format, which holds live notes only; "
                           "they are read as live notes")
    if skipped:
        res.remarks.append(f"{skipped} item(s) of the array are not notes (no text) and were skipped")
    if repeated:
        res.remarks.append(f"{repeated} record(s) repeat an id already seen in the file; their links are not recreated")
    res.summarise = _summarise
    return res


def _summarise(entries: list[Entry], decisions) -> list[str]:
    """One remark that counts every link of the file: recreated (both notes are written by this import) or not."""
    written = {e.source_id for e, d in zip(entries, decisions) if d.outcome in ("new", "version") and e.source_id is not None}
    total = made = 0
    for e, d in zip(entries, decisions):
        for to, _ in e.links:
            total += 1
            if d.outcome in ("new", "version") and e.source_id is not None and to in written:
                made += 1
    if not total:
        return []
    other = total - made
    text = f"{made} of {total} link(s) between notes are recreated (both notes are written by this import)"
    if other:
        text += (f"; {other} {'is' if other == 1 else 'are'} not: one of the two notes is not written by this import (a duplicate, an older version kept "
                 f"as history, a rejected, refused or excluded entry, or a note that is not in the file)")
    return [text]


register(Reader("hermind-json", "Hermind's own JSON export (hermind export): live notes with their fields, older versions kept "
                "as older versions of the new notes, links between the notes and entity aliases; rejected notes are not "
                "imported. The first export format (no status field) needs --from hermind-json",
                detect_hermind_json, read_hermind_json))
