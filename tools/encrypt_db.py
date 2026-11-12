#!/usr/bin/env python3
"""Encrypt (or decrypt) a hermind database with SQLCipher. Needs the optional driver: pip install "hermind[encrypted]".

    # 1. make a key file (random, mode 600, never overwrites)
    python tools/encrypt_db.py --generate-key-file ~/.hermind/db.key
    # 2. stop everything that uses the database (Hermes, hermind-mcp, the vector worker), then:
    export HERMIND_DB_KEY_FILE=~/.hermind/db.key
    python tools/encrypt_db.py ~/.hermes/hermind.db            # writes hermind.db.encrypted, checks it
    python tools/encrypt_db.py ~/.hermes/hermind.db --replace  # ...and swaps it in; the plaintext original is
                                                                       # kept as .plaintext-backup: delete it yourself
    # undo:  python tools/encrypt_db.py DB --decrypt [--replace]

The original is never modified or deleted by this tool. The copy is verified (same tables and row counts, same
user_version, integrity_check) before --replace swaps it in. Put HERMIND_DB_KEY_FILE in the environment of every
process that opens the database (for Hermes: ~/.hermes/.env). Lose the key and the notes are gone; keep a copy of the
key file somewhere other than the database's own disk.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dbdriver  # noqa: E402


def generate_key_file(path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # O_EXCL: never overwrite an existing key
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    print(f"wrote {path} (mode 600). Back it up somewhere else; without it the encrypted notes cannot be read.")
    print(f"then: export HERMIND_DB_KEY_FILE={path}")


def _tables(conn) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}


def convert(src: Path, out: Path, key: str, decrypt: bool) -> None:
    driver = dbdriver._driver()
    if out.exists():
        raise SystemExit(f"{out} already exists; move it away first")
    if decrypt:
        conn = dbdriver.connect(str(src), key, use_env=False)
        target, target_key = "plain", ""
    else:
        conn = driver.connect(str(src))                                # a plaintext source needs no key to open
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target, target_key = "enc", key
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(f"ATTACH DATABASE {dbdriver._quote(str(out))} AS {target} KEY {dbdriver._quote(target_key)}")
    conn.execute(f"SELECT sqlcipher_export('{target}')")
    conn.execute(f"PRAGMA {target}.user_version = {int(uv)}")
    conn.commit()
    conn.execute(f"DETACH DATABASE {target}")
    conn.close()
    os.chmod(out, 0o600)


def verify(src: Path, out: Path, key: str, decrypt: bool) -> None:
    a = dbdriver.connect(str(src), None if not decrypt else key, use_env=False)
    b = dbdriver.connect(str(out), key if not decrypt else None, use_env=False)
    try:
        ta, tb = _tables(a), _tables(b)
        if ta != tb:
            raise SystemExit(f"verification failed: tables differ\n  source {ta}\n  copy   {tb}")
        ua, ub = a.execute("PRAGMA user_version").fetchone()[0], b.execute("PRAGMA user_version").fetchone()[0]
        if ua != ub:
            raise SystemExit(f"verification failed: user_version {ua} != {ub}")
        ok = b.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise SystemExit(f"verification failed: integrity_check says {ok}")
        print(f"verified: {len(ta)} tables, {sum(ta.values())} rows, user_version {ua}, integrity_check ok")
    finally:
        a.close()
        b.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db", nargs="?")
    p.add_argument("--decrypt", action="store_true", help="write a plaintext copy of an encrypted database")
    p.add_argument("--replace", action="store_true", help="swap the verified copy in; keep the original as a backup file")
    p.add_argument("--generate-key-file", metavar="PATH")
    a = p.parse_args()
    if a.generate_key_file:
        generate_key_file(Path(a.generate_key_file))
        return 0
    if not a.db:
        p.error("DB is required")
    src = Path(a.db).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    key = dbdriver.key_from_env()
    if not key:
        raise SystemExit("set HERMIND_DB_KEY_FILE (or HERMIND_DB_KEY) first; make a key file with --generate-key-file")
    state = dbdriver.is_plaintext_file(src)
    if state is None:
        raise SystemExit(f"{src} is empty")
    if a.decrypt == state:
        raise SystemExit(f"{src} is already {'plaintext' if state else 'encrypted'}; nothing to do")
    out = src.with_name(src.name + (".plaintext" if a.decrypt else ".encrypted"))
    convert(src, out, key, a.decrypt)
    verify(src, out, key, a.decrypt)
    if not a.replace:
        print(f"wrote {out}. Original untouched. Re-run with --replace to swap it in.")
        return 0
    backup = src.with_name(src.name + (".encrypted-backup" if a.decrypt else ".plaintext-backup"))
    if backup.exists():
        raise SystemExit(f"{backup} already exists; move it away first")
    os.replace(src, backup)
    for suffix in ("-wal", "-shm"):                                     # leftovers of the old file must not meet the new one
        try:
            os.replace(str(src) + suffix, str(backup) + suffix)
        except FileNotFoundError:
            pass
    os.replace(out, src)
    for f in (src, backup):
        os.chmod(f, 0o600)
    note = "it still holds your notes in plaintext; " if not a.decrypt else ""
    print(f"swapped in. The original is now {backup}: {note}delete it once you have checked the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
