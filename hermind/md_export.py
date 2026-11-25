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

import contextlib
import json
import secrets
import stat
import sys
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


@contextlib.contextmanager
def directory(path, *, create=False, parent_fd=None):
    """Walk directories relative to held descriptors, rejecting symlinks at every step."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("safe export requires POSIX directory descriptors")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if parent_fd is None:
        path = Path(os.path.abspath(path))
        # macOS ships these root-owned links as part of its filesystem layout.
        if sys.platform == "darwin" and path.parts[1:2] in (("tmp",), ("var",)):
            path = Path("/private").joinpath(*path.parts[1:])
        fd = os.open(path.anchor, flags)
        parts = path.parts[1:]
    else:
        path = Path(path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid export subdirectory")
        fd = os.dup(parent_fd)
        parts = path.parts
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def write_at(fd, name, text, *, force):
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("invalid export filename")
    if force:
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(mode):
                raise OSError("export destination must be a regular file")
    temp = ".hermind-" + secrets.token_hex(16)
    out = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        if force:
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


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


def write_folder(store, dest, *, by: str = "kind", include_history: bool = False, scope: str | None = "*",
                 force: bool = False) -> dict:
    root = Path(dest).expanduser().absolute()
    with directory(root, create=True) as fd:
        if not force and os.listdir(fd):
            raise FolderNotEmpty("export directory is not empty; choose an empty folder or pass --force")
        return _write_folder(store, root, fd, by, include_history, scope, force)


def _write_folder(store, root, root_fd, by, include_history, scope, force):
    db = Path(store.db_path)
    notes = collect(store, include_history, scope)
    ids = [n["id"] for n in notes]
    metas = {n["id"]: md_notes.link_meta(store.conn, n) for n in notes}
    ents = store.note_entities(ids, [scope] if scope and scope != "*" else None) if by == "entity" else {}
    by_dir: dict[Path, list[tuple[dict, str]]] = defaultdict(list)
    for n in notes:
        if by == "entity":
            rel_dir = Path(md_notes.slug(n.get("scope") or "default", 30)) / entity_dir(ents.get(n["id"], []), {})
        else:
            rel_dir = Path(md_notes.slug(n["kind"], 20))
        name = f"{n['id']:05d}-{md_notes.slug(l0(n['content'], 60))}.md"
        meta = metas[n["id"]]
        with directory(rel_dir, create=True, parent_fd=root_fd) as fd:
            write_at(fd, name, md_notes.render(n, meta), force=force)
        by_dir[root / rel_dir].append((n, name))
    for d, items in by_dir.items():
        lines = [f"# {d.relative_to(root)}", "", f"{len(items)} notes", ""]
        for n, name in items:
            lines.append(f"- [#{n['id']}]({md_notes.link_target(name)}) {n['kind']} · {md_notes.iso_day(n['created_at'])} · "
                         f"{md_notes.note_status(n)} — {l0(n['content'], 80)}")
        with directory(d.relative_to(root), parent_fd=root_fd) as fd:
            write_at(fd, "index.md", "\n".join(lines) + "\n", force=force)
    top = ["# hermind export", "", f"{len(notes)} notes from {db.name}, layout: by {by}", ""]
    top += [f"- [{d.relative_to(root)}]({md_notes.link_target(d.relative_to(root).as_posix() + '/index.md')}) — {len(items)} notes"
            for d, items in sorted(by_dir.items())]
    write_at(root_fd, "index.md", "\n".join(top) + "\n", force=force)
    write_at(root_fd, MARKER, json.dumps({"db": db.name, "notes": len(notes), "by": by,
                                           "exported": export_time()}) + "\n", force=force)
    by_status: dict[str, int] = {}
    for n in notes:
        by_status[n["status"]] = by_status.get(n["status"], 0) + 1
    return {"notes": len(notes), "folders": len(by_dir), "root": str(root), "by_status": by_status}
