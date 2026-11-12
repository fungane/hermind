"""Optional database encryption for hermind (SQLCipher), behind one function.

Off by default and costs nothing when off: without a key, `connect()` is `sqlite3.connect()` and `sqlcipher3` is never
imported. A key turns it on:

    HERMIND_DB_KEY_FILE=/path/to/keyfile      a file whose content is the passphrase (preferred: not in `ps` or env dumps)
    HERMIND_DB_KEY=passphrase                 the passphrase itself

With a key the database file, its WAL file and the full-text index are encrypted (AES-256, SQLCipher 4). Install the
optional driver with `pip install "hermind[encrypted]"` (the `sqlcipher3` package). Everything that opens the database
(the Hermes plugin, hermind-mcp, the vector worker, the tools) goes through `connect()`, so one key setting covers all.

What it protects: the file leaving the machine (stolen disk, backup, cloud sync). What it does not: anyone who can run
code as the same user and read the key from the environment or the key file.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PLAIN_MAGIC = b"SQLite format 3\x00"
INSTALL_HINT = 'install the optional driver: pip install "hermind[encrypted]"  (package: sqlcipher3)'


class EncryptionError(RuntimeError):
    """The database cannot be opened with the key setting at hand; the message says what to change."""


def _driver():
    try:
        import sqlcipher3
    except ImportError as exc:
        raise EncryptionError(f"a database key is set but the SQLCipher driver is missing; {INSTALL_HINT}") from exc
    return sqlcipher3


def key_from_env(env=None) -> str | None:
    """Passphrase from HERMIND_DB_KEY_FILE (first) or HERMIND_DB_KEY; None when encryption is not configured."""
    env = os.environ if env is None else env
    path = (env.get("HERMIND_DB_KEY_FILE") or "").strip()
    if path:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EncryptionError(f"HERMIND_DB_KEY_FILE={path}: cannot read it ({exc.strerror or exc})") from exc
        if not text:
            raise EncryptionError(f"HERMIND_DB_KEY_FILE={path} is empty")
        return text
    return (env.get("HERMIND_DB_KEY") or "").strip() or None


def is_plaintext_file(path) -> bool | None:
    """True / False for an existing non-empty database file; None when there is nothing to look at yet."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if not head:
        return None
    return head == PLAIN_MAGIC


def _quote(key: str) -> str:
    return "'" + key.replace("'", "''") + "'"


def operational_errors() -> tuple:
    """Exception classes that mean 'database is locked / duplicate column' for whichever driver is loaded."""
    try:
        import sqlcipher3
        return (sqlite3.OperationalError, sqlcipher3.OperationalError)
    except ImportError:
        return (sqlite3.OperationalError,)


def connect(path: str, key: str | None = None, *, use_env: bool = True, **kwargs):
    """Open *path*. With a key the SQLCipher driver is used and the key is checked at once; without one this is
    plain sqlite3, and an encrypted file is refused with a message instead of 'file is not a database'."""
    if key is None and use_env and str(path) != ":memory:":
        key = key_from_env()
    plain = None if str(path).startswith("file:") or str(path) == ":memory:" else is_plaintext_file(path)
    if key is None:
        if plain is False:
            raise EncryptionError(f"{path} is encrypted (or is not a SQLite database at all): if it is encrypted, set HERMIND_DB_KEY_FILE (or HERMIND_DB_KEY) to its key")
        return sqlite3.connect(path, **kwargs)
    if plain is True:
        raise EncryptionError(f"{path} is not encrypted yet but a key is set: encrypt it first with "
                              f"tools/encrypt_db.py, or unset HERMIND_DB_KEY / HERMIND_DB_KEY_FILE")
    driver = _driver()
    conn = driver.connect(path, **kwargs)
    try:
        conn.execute(f"PRAGMA key = {_quote(key)}")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()        # a wrong key fails here, not later
    except Exception as exc:
        conn.close()
        raise EncryptionError(f"{path}: wrong key (or not a SQLCipher 4 database): {exc}") from exc
    return conn


def row_factory(conn):
    """The Row class that belongs to *conn*'s driver."""
    mod = type(conn).__module__.split(".")[0]
    if mod == "sqlcipher3":
        return _driver().Row
    return sqlite3.Row
