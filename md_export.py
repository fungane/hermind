"""Write the notes of a database as a folder of Markdown files (one per note, YAML front matter) with index files.

Used by `hermind export --format markdown`. Read-only on the database. The files can be
read and edited in any editor; edits come back with `tools/import_md.py`, and the whole folder imports with
`hermind import`.

Layout:  by="kind" (default)  OUT/<kind>/00012-first-words.md
         by="entity"          OUT/<scope>/<entity>/00012-first-words.md   (the note's most specific shared entity)
Every directory gets an index.md with one line per note; OUT/index.md lists the directories. Active and pending notes
are exported; include_history adds merged and superseded versions (marked in `status`, passed by on import).
A non-empty folder is refused unless force (which overwrites files of the same name and deletes nothing).
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

try:
    from . import md_notes
    from .store_common import l0
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import md_notes
    from store_common import l0

MARKER = ".hermind-export.json"


class FolderNotEmpty(Exception):
    """The output folder already has files in it and force was not given."""


def _private_write(path: Path, text: str) -> None:
    """Write *text* to *path* readable by the owner only (mode 600), like the JSON export: notes can be sensitive."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)                     # a file that existed (--force) would otherwise keep its old mode


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def export_time() -> str:
    """The export time written into the marker file (a function of its own so a test can fix the clock)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def entity_dir(ents: list, taken: dict) -> str:
    """Most specific entity shared with other notes (smallest document frequency >= 2); '_misc' if none."""
    shared = sorted((e for e in ents if e[2] >= 2), key=lambda e: (e[2], e[1]))
    return md_notes.slug(shared[0][1], 30) if shared else "_misc"


def collect(store, include_history: bool, scope: str | None) -> list[dict]:
    statuses = ("active", "superseded", "merged") if include_history else ("active",)
    sql = f"SELECT * FROM notes WHERE status IN ({','.join('?' * len(statuses))})"
    args: list = list(statuses)
    if scope and scope != "*":
        sql += " AND scope=?"
        args.append(scope)
    return [store._row(r) for r in store.conn.execute(sql + " ORDER BY id", args)]


def check_folder(dest: Path, force: bool) -> None:
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FolderNotEmpty(f"{dest} is not empty; choose a new folder or pass --force (overwrites same-named files)")


def write_folder(store, dest, *, by: str = "kind", include_history: bool = False, scope: str | None = "*",
                 force: bool = False) -> dict:
    """Write the folder. *store* is an open DecisionStore (only read). Returns {"notes", "folders", "root", "by_status"}."""
    dest = Path(dest).expanduser()
    check_folder(dest, force)
    db = Path(store.db_path)
    notes = collect(store, include_history, scope)
    ids = [n["id"] for n in notes]
    metas = {n["id"]: md_notes.link_meta(store.conn, n) for n in notes}
    ents = store.note_entities(ids) if by == "entity" else {}
    _private_dir(dest)
    root = dest.resolve()
    by_dir: dict[Path, list[tuple[dict, str]]] = defaultdict(list)
    for n in notes:
        if by == "entity":
            rel_dir = Path(md_notes.slug(n.get("scope") or "default", 30)) / entity_dir(ents.get(n["id"], []), {})
        else:
            rel_dir = Path(md_notes.slug(n["kind"], 20))
        name = f"{n['id']:05d}-{md_notes.slug(l0(n['content'], 60))}.md"
        path = (root / rel_dir / name).resolve()
        if root not in path.parents:                     # never write outside OUT, whatever the slugs contain
            continue
        meta = metas[n["id"]]
        _private_dir(path.parent)
        _private_write(path, md_notes.render(n, meta))
        by_dir[path.parent].append((n, name))
    for d, items in by_dir.items():
        lines = [f"# {d.relative_to(root)}", "", f"{len(items)} notes", ""]
        for n, name in items:
            lines.append(f"- [#{n['id']}]({name}) {n['kind']} · {md_notes.iso_day(n['created_at'])} · "
                         f"{md_notes.note_status(n)} — {l0(n['content'], 80)}")
        _private_write(d / "index.md", "\n".join(lines) + "\n")
    top = ["# hermind export", "", f"{len(notes)} notes from {db.name}, layout: by {by}", ""]
    top += [f"- [{d.relative_to(root)}]({d.relative_to(root)}/index.md) — {len(items)} notes"
            for d, items in sorted(by_dir.items())]
    _private_write(root / "index.md", "\n".join(top) + "\n")
    _private_write(root / MARKER, json.dumps({"db": str(db), "notes": len(notes), "by": by,
                                           "exported": export_time()}) + "\n")
    by_status: dict[str, int] = {}
    for n in notes:
        by_status[n["status"]] = by_status.get(n["status"], 0) + 1
    return {"notes": len(notes), "folders": len(by_dir), "root": str(root), "by_status": by_status}
