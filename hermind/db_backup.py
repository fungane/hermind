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
import glob
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


_SEQ_WIDTH = 3


def _reserve(db: Path, prefix: str, now: float) -> Path:
    """Create the (empty, owner-only) destination file under a name no other copy has: the name is
    ``<db>.<prefix>-<YYYYmmdd-HHMMSS>-<NNN>``. The counter continues after the highest one already used in that second
    (a pruned copy's number is never reused), so copies made in one second still sort in the order they were made."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = f"{db.name}.{prefix}-{stamp}-"
    seen = [int(seq) for p in db.parent.glob(glob.escape(base) + "*")
            if re.fullmatch(r"[0-9]{3}", seq := p.name[len(base):])]
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
            raise BackupError("the backup copy failed its integrity check")
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
    # Migrations retain copies across schema versions. Only rotate names Hermind
    # generates, including the original timestamp-only format.
    family = r"bak-v[0-9]+" if re.fullmatch(r"bak-v[0-9]+", prefix) else re.escape(prefix)
    pattern = re.compile(re.escape(db.name) + r"\." + family + r"-([0-9]{8}-[0-9]{6})(?:-([0-9]{3}))?")
    candidates = db.parent.glob(prune_glob or f"{glob.escape(db.name)}.{glob.escape(prefix)}-*")
    olds = []
    for path in candidates:
        match = pattern.fullmatch(path.name)
        if match and path != dst and not path.is_symlink() and path.is_file():
            olds.append((match[1], int(match[2] or 0), path))
    olds.sort()
    keep = max(1, keep)
    for _, _, stale in olds[:max(0, len(olds) + 1 - keep)]:    # include the new copy in the limit
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return Backup(dst, removed)
