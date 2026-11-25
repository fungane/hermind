#!/usr/bin/env python3
"""Consistent backup of hermind databases: SQLite online-backup API, integrity_check, newest N kept.

Usage: backup.py [--keep 7] [--dest DIR] DB [DB ...]
Exit code 1 if any database fails the integrity check (the bad copy is discarded and older backups are kept).
"""
import argparse
import os
import re
import sys
import time
import tempfile
import contextlib
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dbdriver  # noqa: E402


def backup_one(src: Path, dest: Path, keep: int) -> bool:
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    tag = src.parent.name if src.parent.name != ".hermes" else "default"
    prefix = f"{tag}-{src.stem}-"
    enc = dbdriver.is_plaintext_file(src) is False
    key = dbdriver.key_from_env() if enc else None
    if enc and not key:
        raise dbdriver.EncryptionError("encrypted database; set HERMIND_DB_KEY_FILE or HERMIND_DB_KEY")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest / f"{prefix}{stamp}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.db"
    with tempfile.TemporaryDirectory(dir=dest) as work:
        candidate = Path(work) / "backup.db"
        with contextlib.closing(dbdriver.connect(src.resolve().as_uri() + "?mode=ro", key, use_env=False, uri=True)) as reader:
            with contextlib.closing(dbdriver.connect(str(candidate), key, use_env=False)) as writer:
                reader.backup(writer)
                if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    print("backup integrity check failed; previous copies retained", file=sys.stderr)
                    return False
                writer.execute("PRAGMA journal_mode=DELETE")
        candidate.chmod(0o600)
        os.link(candidate, out)
    # Literal filename prefix. Brackets or other glob characters in a database
    # name must never select a different database's backups for removal.
    stamp_pattern = re.compile(re.escape(prefix) + r"[0-9]{8}-[0-9]{6}(?:-[0-9]+-[0-9a-f]{8})?\.db")
    olds = sorted(p for p in dest.iterdir() if stamp_pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink())
    for old in olds[:-keep]:
        old.unlink(missing_ok=True)
    print(f"ok {out} ({out.stat().st_size // 1024} KB), kept {min(len(olds), keep)}")
    return True


def _keep(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of backups to keep (1 or more)")
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("dbs", nargs="+")
    p.add_argument("--keep", type=_keep, default=7, help="how many of the newest backups to keep (1 or more, default 7)")
    old, new = Path.home() / "backups" / "decision-memory", Path.home() / "backups" / "hermind"
    p.add_argument("--dest", default=str(old if old.exists() and not new.exists() else new))
    a = p.parse_args()
    os.umask(0o077)
    results = []
    for d in a.dbs:
        if not Path(d).exists():
            print(f"backup.py: database not found: {d}", file=sys.stderr)
            results.append(False)
            continue
        results.append(backup_one(Path(d).resolve(), Path(a.dest), a.keep))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
