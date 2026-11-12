#!/usr/bin/env python3
"""Export hermind notes to a folder of Markdown files (one per note, YAML front matter), for reading, grepping,
editing in any editor, or versioning in git. Read-only on the database. Edit the files and bring the changes back with
tools/import_md.py.

    python tools/export_md.py --db ~/.hermes/hermind.db --out ~/hermind-notes [--by kind|entity]
                              [--include-hidden] [--scope NAME|*] [--force]

Layout:  --by kind (default)  OUT/<kind>/00012-first-words.md
         --by entity          OUT/<scope>/<entity>/00012-first-words.md   (the note's most specific shared entity)
Every directory gets an index.md with one line per note; OUT/index.md lists the directories. Active and pending notes
are exported; --include-hidden adds merged and superseded versions (marked in `status`, ignored by import).
Refuses to write into a non-empty directory unless --force (which overwrites files of the same name, deletes nothing).

The writing code lives in md_export.py (repository root), which `hermind export --format markdown` uses too; this script is
a thin wrapper with the same options and output.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import md_export  # noqa: E402  (the writing code, in the repository root so `hermind export --format markdown` ships in the wheel)
import mdnotes  # noqa: E402,F401  (a name for md_notes.py in the repository root; kept importable from here)
from store import DecisionStore  # noqa: E402

MARKER = md_export.MARKER
entity_dir = md_export.entity_dir
collect = md_export.collect


def _default_db() -> str:
    """$HERMES_HOME/hermind.db, or the old decision_memory.db when that is what is there."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    old, new = os.path.join(home, "decision_memory.db"), os.path.join(home, "hermind.db")
    return old if os.path.exists(old) and not os.path.exists(new) else new


def run(args, out=None) -> int:
    out = out or sys.stdout
    db, dest = Path(args.db).expanduser(), Path(args.out).expanduser()
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 2
    try:
        md_export.check_folder(dest, args.force)
    except md_export.FolderNotEmpty as e:
        print(e, file=sys.stderr)
        return 2
    store = DecisionStore(str(db))
    try:
        res = md_export.write_folder(store, dest, by=args.by, include_history=args.include_hidden, scope=args.scope,
                                     force=args.force)
    finally:
        store.close()
    print(f"exported {res['notes']} notes into {res['folders']} folder(s) under {res['root']}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=_default_db())
    p.add_argument("--out", required=True)
    p.add_argument("--by", choices=("kind", "entity"), default="kind")
    p.add_argument("--include-hidden", action="store_true", help="also export merged and superseded notes")
    p.add_argument("--scope", default="*", help="only this scope (default: all)")
    p.add_argument("--force", action="store_true", help="write into a non-empty folder (overwrites same-named files)")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
