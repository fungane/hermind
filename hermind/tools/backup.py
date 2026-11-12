#!/usr/bin/env python3
"""Consistent backup of hermind databases: SQLite online-backup API, integrity_check, newest N kept.

Usage: backup.py [--keep 7] [--dest DIR] DB [DB ...]
Exit code 1 if any database fails the integrity check (the bad copy is discarded and older backups are kept).
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dbdriver  # noqa: E402


def backup_one(src: Path, dest: Path, keep: int) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    tag = src.parent.name if src.parent.name != ".hermes" else "default"
    out = dest / f"{tag}-{src.stem}-{time.strftime('%Y%m%d-%H%M')}.db"
    enc = dbdriver.is_plaintext_file(src) is False              # encrypted source: the backup stays encrypted, same key
    key = dbdriver.key_from_env() if enc else None
    if enc and not key:
        raise dbdriver.EncryptionError(f"{src} is encrypted: set HERMIND_DB_KEY_FILE (or HERMIND_DB_KEY)")
    a = dbdriver.connect(f"file:{src}?mode=ro", key, use_env=False, uri=True)
    b = dbdriver.connect(str(out), key, use_env=False)
    try:
        a.backup(b)
        ok = b.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        b.close()
        a.close()
    if ok != "ok":
        out.unlink(missing_ok=True)
        print(f"FAILED integrity_check for {src}: {ok}", file=sys.stderr)
        return False
    os.chmod(out, 0o600)
    olds = sorted(dest.glob(f"{tag}-{src.stem}-*.db"))
    for old in olds[:-keep]:
        old.unlink()
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
