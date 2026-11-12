#!/usr/bin/env python3
"""Bring edits made to exported Markdown notes (`hermind export --format markdown`) back into the database.

    python tools/import_md.py --db ~/.hermes/hermind.db DIR            # dry run (default): only reports
    python tools/import_md.py --db ... DIR --apply [--stamp-ids] [--force]

* A file belongs to note #id of this database only when it carries both `id` and `rev` and that `rev` is the fingerprint
  of the note's current version or of one of its earlier versions. Anything else (an id this database does not have,
  a `rev` that never belonged to that note, no `rev`) means the id is someone else's numbering: the file is treated
  like a file without an id, i.e. as a new note.
* A file that belongs to a note is matched to it. If the text or keywords differ, the change goes through the normal
  versioned update (the old text stays in the note's history, the search index and entity links are rebuilt). Edits to
  kind, tags, trust, valid_from and valid_to are applied as metadata; `status: active` on a pending note confirms it.
  id, created, supersedes, outdated_by, pinned and scope are read-only and ignored.
* A file that does not belong to a note becomes a new note (the front matter is optional). New / duplicate / refused
  is decided by the same import core `hermind import` uses, so a dry run reports what --apply then does. --stamp-ids rewrites that file in the
  export format (with its new id) so a second import does not see it as new again (an exact duplicate is detected
  anyway).
* After --apply, each file that was updated is rewritten from the database (same text, fresh `rev` and validity), so the
  folder stays in sync and importing it again is a no-op.
* Conflicts: each exported file carries a `rev` fingerprint. If a file was edited AND the note changed in the database
  since the export, the file is skipped and reported; --force lets the file win.
* The files of the folder are chosen by the same function as `hermind import`: .md, .markdown, .mdx and .txt files, except an
  index.md that the Markdown export wrote and anything in a hidden folder. A new file gets the same text as with `hermind import`
  (its title / name / file name and description in front of a body that lacks them; --ignore-file-names turns file names off),
  and files that are not UTF-8 need --encoding NAME (UTF-8 and UTF-16/32 with a byte-order mark are always read).
* Hidden (merged / superseded / rejected) notes, exporter-written `index.md`, secrets and unreadable files are skipped and
  reported. Nothing is ever deleted: removing a file does nothing.
"""
from __future__ import annotations

import argparse
import codecs
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import md_notes as mdnotes  # noqa: E402  (md_notes.py in the repository root)
import import_core  # noqa: E402
from import_readers import md_entry  # noqa: E402
from store import DecisionStore, RefusedWrite  # noqa: E402
from store_writes import FORGOTTEN_REFUSAL, check_text, screen_fields  # noqa: E402


def _default_db() -> str:
    """$HERMES_HOME/hermind.db, or the old decision_memory.db when that is what is there."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    old, new = os.path.join(home, "decision_memory.db"), os.path.join(home, "hermind.db")
    return old if os.path.exists(old) and not os.path.exists(new) else new


def find_files(root: Path) -> list[Path]:
    return mdnotes.find_files(root)


_day10 = mdnotes.day10


def check_update(store: DecisionStore, f: dict, changed) -> None:
    """The store's refusals for an edited-back note (text, keywords, forgotten text, credential-like tags), raised before
    anything is written: the plan and the apply step ask the same question, so a refusal leaves the note unchanged."""
    if "content" in changed or "keywords" in changed:
        check_text(f["content"], f["keywords"])
        if store.is_forgotten(f["content"]):
            raise RefusedWrite(FORGOTTEN_REFUSAL)
    if "tags" in changed:
        screen_fields(f["tags"])


def plan_file(path: Path, root: Path, store: DecisionStore, seen: set, force: bool, encoding: str | None = None) -> dict:
    rel = path.relative_to(root).as_posix()
    item = {"path": path, "rel": rel, "action": "skip", "why": ""}
    try:
        text, why = mdnotes.decode_text(path.read_bytes(), encoding)
    except OSError as exc:
        return {**item, "action": "invalid", "why": f"unreadable ({exc.strerror or exc})"}
    if text is None:
        return {**item, "action": "invalid", "why": f"unreadable: {why}"}
    meta, body = mdnotes.parse_note_file(text)
    nid = meta.get("id")
    if nid is not None and not (isinstance(nid, int) and not isinstance(nid, bool)):
        return {**item, "action": "invalid", "why": f"id is not a number: {nid!r}"}
    try:
        norm = {k: _day10(meta[k]) for k in ("valid_from", "valid_to") if k in meta}
        bad_dates = None
    except ValueError as exc:
        norm, bad_dates = {}, str(exc)
    note = store.get(nid) if nid is not None else None
    owned = None
    if note is not None and meta.get("rev"):
        owned = mdnotes.owner_of(meta, import_core.View(store.conn).note_versions(nid))
    if owned is None:
        # no id, an id this database does not have, or an id that is someone else's numbering (no matching `rev`):
        # an ordinary file; the import core decides new / duplicate / refused for it in run()
        return {**item, "action": "candidate", "meta": meta, "body": body, "stem": path.stem}
    if bad_dates:
        return {**item, "action": "invalid", "why": bad_dates}
    if nid in seen:
        return {**item, "action": "skip", "why": f"id #{nid} already seen in another file"}
    seen.add(nid)
    if note["status"] != "active":
        return {**item, "action": "skip", "why": f"#{nid} is {note['status']}; only active / pending notes are imported"}
    if not body:
        return {**item, "action": "invalid", "why": "empty text (to forget a note use memory_forget)"}
    db_f, f, changed = mdnotes.compare_file(note, {**meta, **norm}, body)
    if not changed:
        return {**item, "action": "same", "id": nid}
    file_rev, db_rev = meta.get("rev"), mdnotes.rev_of(db_f)
    if file_rev and mdnotes.rev_of(f) == file_rev:      # the file is the untouched export; the database moved on
        return {**item, "action": "same", "id": nid, "why": "file is an older export; database is newer"}
    if file_rev and file_rev != db_rev and not force:
        return {**item, "action": "conflict", "id": nid, "changed": changed,
                "why": "edited in the file and changed in the database since the export (--force: file wins)"}
    try:                                                   # the update would be refused: say so now, not at --apply
        check_update(store, f, changed)
    except (RefusedWrite, ValueError) as exc:
        return {**item, "action": "refused", "why": str(exc)}
    return {**item, "action": "update", "id": nid, "changed": changed, "fields": f, "note": note}


def apply_item(store: DecisionStore, item: dict, stamp: bool) -> tuple[str, str]:
    """Perform one planned change; returns (outcome, detail)."""
    if item["action"] == "update":
        f, nid, changed = item["fields"], item["id"], item["changed"]
        check_update(store, f, changed)                    # all refusals first: a refused edit changes nothing
        if "content" in changed or "keywords" in changed:
            store.update(nid, f["content"], f["keywords"])
        meta_kw = {}
        if "kind" in changed:
            meta_kw["kind"] = f["kind"]
        if "tags" in changed:
            meta_kw["tags"] = f["tags"]
        if "trust" in changed:
            meta_kw["trust"] = f["trust"]
        for k in ("valid_from", "valid_to"):
            if k in changed:
                meta_kw[k] = f[k] or None
        if meta_kw:
            store.set_fields(nid, **meta_kw)
        if "pending" in changed and not f["pending"]:
            store.confirm(nid)
        fresh = store.get(nid)       # keep the folder in sync: rewrite this file from the database (new rev, validity)
        item["path"].write_text(mdnotes.render(fresh, mdnotes.link_meta(store.conn, fresh)), encoding="utf-8")
        return "updated", ""
    p = item["plan"]
    nid, created = store.add(p.content, kind=p.kind, keywords=p.keywords, tags=p.tags, valid_from=p.valid_from,
                             valid_to=p.valid_to, review=p.review, pinned=p.pinned, scope=p.scope)
    if not created:
        return "duplicate", f"same text as #{nid}"
    if p.trust is not None and abs(p.trust - 0.5) > 1e-9:
        store.set_fields(nid, trust=p.trust)
    if stamp:       # the file becomes a canonical export file of the new note (id, rev, ...)
        fresh = store.get(nid)
        item["path"].write_text(mdnotes.render(fresh, mdnotes.link_meta(store.conn, fresh)), encoding="utf-8")
    return "created", f"#{nid}"


def decide_candidates(items: list[dict], store: DecisionStore, ignore_file_names: bool = False) -> None:
    """Turn every "candidate" (a file that does not belong to a note) into new / skip / refused / invalid with the
    import core, the same function `hermind import` previews with, so a dry run and --apply agree. A new item carries
    the core's plan (kind, dates, trust, text ... as they will be written). Changes the items in place."""
    cands = [it for it in items if it["action"] == "candidate"]
    entries = []
    for it in cands:
        entry = md_entry(it["meta"], it["body"], it["rel"], it["stem"], ignore_file_names=ignore_file_names)
        entry.created = None            # this tool has always stamped a new note with the time of the import
        entries.append(entry)
    for it, d in zip(cands, import_core.decide(entries, import_core.View(store.conn), reader="markdown")):
        if d.outcome == "new":
            it["action"], it["plan"], it["why"] = "new", d.plan, d.reason
        elif d.outcome == "refused":
            it["action"], it["why"] = "refused", d.reason
        elif d.outcome == "unreadable":
            it["action"], it["why"] = "invalid", d.reason
        else:                           # duplicate, history, empty
            it["action"], it["why"] = "skip", "empty file" if d.outcome == "empty" else d.reason


def run(args, out=None) -> int:
    out = out or sys.stdout
    db, root = Path(args.db).expanduser(), Path(args.dir).expanduser()
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    if args.encoding:
        try:
            codecs.lookup(args.encoding)
        except LookupError:
            print(f"unknown encoding: {args.encoding}", file=sys.stderr)
            return 2
    store = DecisionStore(str(db))
    counts = {"same": 0, "update": 0, "new": 0, "conflict": 0, "skip": 0, "invalid": 0, "refused": 0}
    lines = []
    try:
        seen: set = set()
        walk = mdnotes.walk_folder(root)
        items = [plan_file(p, root, store, seen, args.force, args.encoding) for p in walk.notes]
        items += [{"path": p, "rel": p.relative_to(root).as_posix() + ("/" if p.is_dir() else ""), "action": "skip",
                   "why": mdnotes.SYMLINK_REASON} for p in walk.links]      # never followed: counted as skipped
        decide_candidates(items, store, args.ignore_file_names)
        for it in items:
            act = it["action"]
            if act == "same":
                counts["same"] += 1
                continue
            detail = it.get("why", "")
            if act in ("update", "new"):
                label = "update" if act == "update" else "new   "
                what = ", ".join(it["changed"]) if act == "update" else "new note from file"
                if args.apply:
                    try:
                        outcome, extra = apply_item(store, it, args.stamp_ids)
                    except (RefusedWrite, ValueError) as exc:
                        counts["refused"] += 1
                        lines.append(f"  refused  {it['rel']}: {exc}")
                        continue
                    if outcome == "duplicate":
                        counts["skip"] += 1
                        lines.append(f"  skip     {it['rel']}: {extra}")
                        continue
                    lines.append(f"  {outcome:<8} {it['rel']}  {('#%d ' % it['id']) if act == 'update' else extra} ({what})")
                else:
                    lines.append(f"  {label}   {it['rel']}  {('#%d ' % it['id']) if act == 'update' else ''}({what})")
                counts[act] += 1
            else:
                counts[act] += 1
                lines.append(f"  {act:<8} {it['rel']}: {detail}")
        store.conn.commit()
    finally:
        store.close()
    mode = "APPLIED" if args.apply else "DRY RUN (nothing was changed; use --apply)"
    print(f"import_md - {mode}   folder: {root}", file=out)
    print("\n".join(lines) if lines else "  no changes", file=out)
    print(f"summary: {counts['same']} unchanged, {counts['update']} {'updated' if args.apply else 'to update'}, "
          f"{counts['new']} {'created' if args.apply else 'new'}, {counts['conflict']} conflict(s), "
          f"{counts['skip']} skipped, {counts['invalid']} invalid, {counts['refused']} refused", file=out)
    return 1 if counts["conflict"] or counts["invalid"] or counts["refused"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=_default_db())
    p.add_argument("dir", help="folder written by `hermind export --format markdown` (or any folder of .md files)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="only report (the default)")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    p.add_argument("--stamp-ids", action="store_true", help="write the new id into files that had none (with --apply)")
    p.add_argument("--force", action="store_true", help="on a conflict the file wins")
    p.add_argument("--encoding", metavar="NAME", help="the encoding of files that are not valid UTF-8 (big5, gb18030, cp1252, "
                   "shift_jis ...); UTF-8 and UTF-16/32 with a byte-order mark are always read")
    p.add_argument("--ignore-file-names", action="store_true", help="do not add a file name in front of a note that does not say it")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
