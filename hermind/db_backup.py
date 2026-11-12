"""One backup routine for Hermind databases, built on SQLite's online backup (``Connection.backup``).

It copies everything a new reader would see, including what is still in the write-ahead log: it needs no checkpoint
and waits for no reader (a checkpoint stops at the oldest open read transaction, so a copy of the main file can miss
the newest notes; the online backup does not). The copy is checked with ``PRAGMA integrity_check``, made owner-only,
keeps an encrypted database encrypted with the same key, and leaves no file behind when it fails.

Used by ``import_core`` (import and revert) and ``store_schema`` (the copy made before a schema upgrade). Standard
library only.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from . import dbdriver
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import dbdriver


class BackupError(Exception):
    """The copy could not be made (nothing is left behind)."""


@dataclass
class Backup:
    path: Path
    removed: int = 0          # older copies of the same kind that were deleted to keep only the newest ones


_STAMP_END = re.compile(r"(\d{8}-\d{6})(?:-(\d+))?$")
_SEQ_WIDTH = 3


def _order_key(p: Path):
    """Backups sort in the order they were made: UTC-free local stamp, then the counter for copies made in the same
    second. Names that do not end in a stamp (made by hand) sort first, among themselves by modification time."""
    m = _STAMP_END.search(p.name)
    if m:
        return (1, m.group(1), int(m.group(2) or 0), 0.0)
    try:
        return (0, "", 0, p.stat().st_mtime)
    except OSError:
        return (0, "", 0, 0.0)


def _reserve(db: Path, prefix: str, now: float) -> Path:
    """Create the (empty, owner-only) destination file under a name no other copy has: the name is
    ``<db>.<prefix>-<YYYYmmdd-HHMMSS>-<NNN>``. The counter continues after the highest one already used in that second
    (a pruned copy's number is never reused), so copies made in one second still sort in the order they were made."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    seen = [int(m.group(1)) for p in db.parent.glob(f"{db.name}.{prefix}-{stamp}-*")
            if (m := re.search(r"-(\d+)$", p.name))]
    for seq in range(max(seen, default=0) + 1, 10 ** _SEQ_WIDTH):
        dst = db.with_name(f"{db.name}.{prefix}-{stamp}-{seq:0{_SEQ_WIDTH}d}")
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return dst
    raise BackupError(f"too many backups of {db.name} in one second")


def _copy(src, dst) -> None:
    """The online backup itself (one place to look at, and to replace in a test that simulates a failure)."""
    src.backup(dst)


def _integrity(conn) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def backup_database(db: Path | str, *, prefix: str = "bak-import", keep: int = 5, key: str | None = None,
                    prune_glob: str | None = None, now: float | None = None) -> Backup | None:
    """Copy *db* to ``<db>.<prefix>-<stamp>-<NNN>`` and return Backup(path, removed); None when there is no database
    file yet (or it is empty). Afterwards only the newest *keep* copies matching *prune_glob* (default
    ``<db>.<prefix>-*``) stay. Raises BackupError when the copy cannot be made or fails its integrity check; the
    partial file is removed and no older copy is touched."""
    db = Path(db).expanduser()
    try:
        if not db.exists() or db.stat().st_size == 0:
            return None
    except OSError as exc:
        raise BackupError(f"cannot look at {db}: {exc.strerror or exc}") from exc
    plain = dbdriver.is_plaintext_file(db)
    if plain is None:
        return None
    key = None if plain else (key if key is not None else dbdriver.key_from_env())
    if plain is False and key is None:
        raise BackupError(f"{db} is encrypted: set HERMIND_DB_KEY_FILE (or HERMIND_DB_KEY) to its key")
    try:
        dst = _reserve(db, prefix, time.time() if now is None else now)
    except OSError as exc:
        raise BackupError(f"cannot create a backup next to {db}: {exc.strerror or exc}") from exc
    a = b = None
    try:
        a = dbdriver.connect(f"{db.resolve().as_uri()}?mode=ro", key, use_env=False, uri=True)
        b = dbdriver.connect(str(dst), key, use_env=False)
        _copy(a, b)
        verdict = _integrity(b)
        if verdict != "ok":
            raise BackupError(f"the copy of {db.name} failed its integrity check: {verdict}")
        b.close()
        b = None
        os.chmod(dst, 0o600)
    except BaseException as exc:
        for c in (b, a):
            if c is not None:
                with contextlib.suppress(Exception):
                    c.close()
        a = None
        for leftover in (dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm"), Path(str(dst) + "-journal")):
            with contextlib.suppress(OSError):
                leftover.unlink()
        if isinstance(exc, BackupError) or not isinstance(exc, Exception):
            raise
        raise BackupError(f"could not back up {db.name}: {getattr(exc, 'strerror', None) or exc}") from exc
    finally:
        if a is not None:
            with contextlib.suppress(Exception):
                a.close()
    with contextlib.suppress(OSError):                   # the copy is closed, so no -wal / -shm of it should remain
        for side in ("-wal", "-shm"):
            Path(str(dst) + side).unlink(missing_ok=True)
    removed = 0
    olds = sorted((p for p in db.parent.glob(prune_glob or f"{db.name}.{prefix}-*") if p != dst and p.is_file()),
                  key=_order_key)
    keep = max(1, keep)
    for stale in olds[:max(0, len(olds) + 1 - keep)]:    # +1: the copy just made counts among the kept ones
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return Backup(dst, removed)
