"""The import core: every source goes through here on its way into a Hermind database.

A **reader** (see import_readers.py) turns a source into **entries**; the core decides one **outcome** per entry, shows
the decisions as a preview, and with ``apply=True`` writes the ``new`` ones as one **batch** that can be listed and
reverted. The promises, in short:

* every entry gets exactly one outcome (new / version / duplicate / edited / history / refused / unreadable / empty /
  excluded)
  and every outcome other than
  ``new`` is reported with its origin and its reason;
* a preview and an apply decide with the same function (`decide`); apply only adds the writes;
* existing notes are never touched (no overwrite, no keyword merge, no proof_count bump);
* imported notes are ordinary live notes that never expire;
* a batch can be reverted: the notes it created are removed (without a tombstone), notes changed since stay;
* the database is backed up before the first write.

Nothing here needs a network, an LLM or a package outside the standard library. Previews, ``--list`` and a revert
preview open the database read-only and change nothing in it; the batch tables are created by the first applied import.
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

try:
    from . import dbdriver
    from .db_backup import BackupError, backup_database
    from .store_common import KINDS, RefusedWrite, TRUST_DEFAULT, _clean_scope, _digest, _entity_key, classify_kind, parse_valid
    from .store_writes import FORGOTTEN_REFUSAL, check_text
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/)
    import dbdriver
    from db_backup import BackupError, backup_database
    from store_common import KINDS, RefusedWrite, TRUST_DEFAULT, _clean_scope, _digest, _entity_key, classify_kind, parse_valid
    from store_writes import FORGOTTEN_REFUSAL, check_text

OUTCOMES = ("new", "version", "duplicate", "edited", "history", "refused", "unreadable", "empty", "excluded")   # the fixed order of every count
CHUNK = 300                       # notes per commit while applying: other writers are never locked out for long
DEFAULT_SCOPE = "default"         # what `hermind remember` uses (DecisionStore's default scope)
HISTORY_STATUSES = ("superseded", "merged")      # how tools/export_md.py --include-hidden marks an older version
VERSION_RELS = ("version_of", "superseded_by", "merged_into")   # the links from an older version to a newer note
EDIT_SYNC_HINT = "tools/import_md.py syncs edits of an exported folder"


# -- entries and readers ----------------------------------------------------------------------------------------

@dataclass
class Entry:
    """One candidate note. *origin* is a stable, human-readable place in the source (a relative path, ``USER.md#3``)."""
    text: str
    origin: str
    kind: str | None = None
    keywords: str | Iterable[str] = ""
    created: float | str | None = None        # only when the source itself states a date for the entry
    tags: Iterable[str] = ()
    pinned: bool = False
    source_session: str | None = None
    valid_from: float | str | None = None
    valid_to: float | str | None = None
    pending: bool = False                     # keep the note in the review queue (the one case that is not live)
    owner_id: int | None = None               # the entry is a copy of this database's note #owner_id ...
    owner_relation: str = ""                  # ... "same" or "edited" (the reader knows how to tell; see import_readers)
    owner_detail: str = ""
    body: str | None = None                   # a reader that adds a subject to the text also gives the body alone: the
                                              # rules "empty", "nothing outside <private>" and "forgotten" judge the body
    trust: float | str | None = None          # 0..1; anything else is dropped (and said) by the core
    scope: str | None = None                  # decided and written in this scope (default: the run's scope)
    status: str = ""                          # "superseded" / "merged": an older version kept for history, not imported
    history: str = ""                         # a reader's own reason: this entry is an older version, kept for history
    unreadable: str = ""                      # the text could not be decoded: the reason (and how to make it readable)
    # -- what Hermind's own JSON export carries (reader_hermind_json.py); every other reader leaves these alone --
    source_id: int | None = None              # the entry's id in the source (the source numbers its notes)
    version_of: int | None = None             # an older version of the live entry with this source_id (outcome `version`)
    version_rel: str = ""                     # the link from the older version to its note: version_of, superseded_by, merged_into
    after: dict | None = None                 # set on the new note after it is written (see DecisionStore.restore_fields)
    entities: tuple | None = None             # ((name, (alias, ...)), ...): exactly the entities the source gives the note
                                              # (None: the source says nothing, the store finds them in the text)
    links: tuple = ()                         # ((source id of another note, rel), ...): the outgoing links the source gives


@dataclass
class ReadResult:
    entries: list[Entry] = field(default_factory=list)
    remarks: list[str] = field(default_factory=list)   # what the reader saw and did not turn into entries
    groups: list[tuple[str, str]] = field(default_factory=list)   # the parts of the source: (name, origin glob), in order
    summarise: Callable | None = None                  # (entries, decisions) -> remarks that depend on the outcomes


@dataclass
class Reader:
    name: str
    summary: str                                       # one line: what it accepts
    detect: Callable[[Path], int]                      # 0 = not mine; higher = more specific match
    read: Callable[..., ReadResult]                    # read(source, view[, **options])
    options: tuple[str, ...] = ()                      # the option names this reader takes (see run_import)


_READERS: dict[str, Reader] = {}


def glob_escape(name: str) -> str:
    """*name* as a glob that matches only itself: ``[``, ``]``, ``*`` and ``?`` are written ``[[]``, ``[]]``, ``[*]``, ``[?]``."""
    return re.sub(r"([\[\]*?])", r"[\1]", name)


def register(reader: Reader) -> Reader:
    """Add a reader to the registry (one function plus one call, see import_readers.py)."""
    _READERS[reader.name] = reader
    return reader


_BUILTINS_LOADED = False


def readers() -> dict[str, Reader]:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        _BUILTINS_LOADED = True
        try:
            from . import import_readers  # noqa: F401  (registers the built-in readers)
        except ImportError:
            import import_readers  # noqa: F401
    return dict(_READERS)


def detect_reader(source: Path) -> tuple[str | None, list[str]]:
    """(name, []) for a unique best match; (None, names) when several readers match equally; (None, []) for none."""
    scores = {n: r.detect(source) for n, r in readers().items()}
    best = max(scores.values(), default=0)
    if best <= 0:
        return None, []
    top = sorted(n for n, s in scores.items() if s == best)
    return (top[0], []) if len(top) == 1 else (None, top)


# -- looking at a database without changing it ------------------------------------------------------------------

class EmptyView:
    """A database that does not exist yet: nothing is in it."""
    exists = False

    def forgotten(self, digest: str) -> bool:
        return False

    def find(self, digest: str, scope: str):
        return None

    def note_versions(self, note_id: int) -> list[dict]:
        return []

    def batches(self) -> list[dict]:
        return []

    def batch_notes(self, batch_id: str) -> list[dict]:
        return []

    def note_state(self, note_id: int) -> dict | None:
        return None

    def close(self) -> None:
        pass


class View(EmptyView):
    """Read-only questions about an existing database. The connection is opened ``mode=ro`` with ``query_only``, so a
    preview cannot write to it even by mistake."""
    exists = True

    def __init__(self, conn):
        self.conn = conn
        self._tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")} if "notes" in self._tables else set()

    def forgotten(self, digest: str) -> bool:
        return "tombstones" in self._tables and \
            self.conn.execute("SELECT 1 FROM tombstones WHERE digest=?", (digest,)).fetchone() is not None

    def find(self, digest: str, scope: str):
        """The note add() would call the same text: any status (merged, superseded and rejected notes keep their hash)."""
        if "notes" not in self._tables:
            return None
        status = "status" if "status" in self._cols else "'active'"
        review = "review" if "review" in self._cols else "'active'"
        scope_sql, args = ("AND scope=?", (digest, scope)) if "scope" in self._cols else ("", (digest,))
        r = self.conn.execute(
            f"SELECT id, {status} AS status, {review} AS review FROM notes WHERE content_hash=? {scope_sql} "
            f"ORDER BY ({status}='active') DESC, id LIMIT 1", args).fetchone()
        return {"id": r[0], "status": r[1], "review": r[2]} if r else None

    def note_versions(self, note_id: int) -> list[dict]:
        """The current note and its earlier versions (the hidden history rows), each as a dict of columns."""
        if "notes" not in self._tables:
            return []
        self.conn.row_factory = dbdriver.row_factory(self.conn)
        rows = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchall()
        if rows and "note_links" in self._tables:
            rows += self.conn.execute(
                "SELECT n.* FROM notes n JOIN note_links l ON l.src=n.id WHERE l.dst=? AND l.rel='version_of' "
                "ORDER BY n.id DESC", (note_id,)).fetchall()
        return [dict(zip(r.keys(), tuple(r))) for r in rows]

    def batches(self) -> list[dict]:
        if "import_batches" not in self._tables:
            return []
        out = []
        for r in self.conn.execute("SELECT id, reader, source, created_at, counts, complete, reverted_at "
                                   "FROM import_batches ORDER BY id"):
            live = self.conn.execute("SELECT COUNT(*) FROM import_batch_notes WHERE batch_id=? AND removed_at IS NULL",
                                     (r[0],)).fetchone()[0]
            total = self.conn.execute("SELECT COUNT(*) FROM import_batch_notes WHERE batch_id=?", (r[0],)).fetchone()[0]
            if r[6] is not None:
                state = "partly reverted" if live else "reverted"
            else:
                state = "complete" if r[5] else "incomplete"
            out.append({"batch": r[0], "reader": r[1], "source": r[2], "created": _iso(r[3]),
                        "counts": json.loads(r[4] or "{}"), "state": state, "notes_created": total,
                        "notes_remaining": live})
        return out

    def batch_notes(self, batch_id: str) -> list[dict]:
        if "import_batch_notes" not in self._tables:
            return []
        return [{"note_id": r[0], "origin": r[1], "content_hash": r[2], "state": json.loads(r[3] or "{}"),
                 "proof_count": r[4]} for r in self.conn.execute(
            "SELECT note_id, origin, content_hash, state, proof_count FROM import_batch_notes "
            "WHERE batch_id=? AND removed_at IS NULL ORDER BY note_id", (batch_id,))]

    def batch_exists(self, batch_id: str) -> bool:
        return "import_batches" in self._tables and \
            self.conn.execute("SELECT 1 FROM import_batches WHERE id=?", (batch_id,)).fetchone() is not None

    def note_state(self, note_id: int) -> dict | None:
        """The columns revert compares, or None when the note is gone."""
        if "notes" not in self._tables:
            return None
        r = self.conn.execute("SELECT content_hash, keywords, tags, kind, pinned, review, valid_from, valid_to, status, "
                              "canonical_id, proof_count FROM notes WHERE id=?", (note_id,)).fetchone()
        if r is None:
            return None
        d = dict(zip(("content_hash", "keywords", "tags", "kind", "pinned", "review", "valid_from", "valid_to",
                      "status", "canonical_id", "proof_count"), tuple(r)))
        d["links"] = sorted({x[0] for x in self.conn.execute("SELECT rel FROM note_links WHERE src=? OR dst=?",
                                                              (note_id, note_id))}) if "note_links" in self._tables else []
        return d

    def close(self) -> None:
        self.conn.close()


def _iso(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def open_view(db: Path | str, key: str | None = None):
    """A read-only view of *db* (an EmptyView when the file does not exist). Raises dbdriver.EncryptionError for an
    encrypted file without a key (or a file that is not a database), sqlite3.Error for a damaged one."""
    db = Path(db).expanduser()
    if not db.exists():
        return EmptyView()
    key = key if key is not None else dbdriver.key_from_env()
    plain = dbdriver.is_plaintext_file(db)
    if plain is None:                                    # an empty file: nothing in it
        return EmptyView()
    if key is None and plain is False:
        raise dbdriver.EncryptionError(f"{db} is encrypted (or is not a SQLite database at all): if it is encrypted, "
                                       f"set HERMIND_DB_KEY_FILE (or HERMIND_DB_KEY) to its key")
    if key is not None and plain is True:
        raise dbdriver.EncryptionError(f"{db} is not encrypted yet but a key is set: unset HERMIND_DB_KEY / "
                                       f"HERMIND_DB_KEY_FILE, or encrypt it first with tools/encrypt_db.py")
    conn = dbdriver.connect(f"{db.resolve().as_uri()}?mode=ro", key, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        return View(conn)
    except Exception:
        conn.close()
        raise


# -- deciding ---------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    """What apply will pass to DecisionStore.add for a ``new`` entry."""
    content: str
    keywords: str
    kind: str
    created_at: float | None
    tags: list[str]
    pinned: bool
    source_session: str
    valid_from: float | None
    valid_to: float | None
    review: str
    trust: float | None = None
    scope: str | None = None
    notes: list[str] = field(default_factory=list)       # metadata that could not be used and was dropped, and why
    source_id: int | None = None
    after: dict | None = None
    entities: tuple | None = None
    links: tuple = ()
    version: dict | None = None                          # an older version: {"of": source id, "status", "rel", "origin"}


@dataclass
class Decision:
    n: int                                # 1-based position in the source
    origin: str
    outcome: str
    reason: str = ""
    note_id: int | None = None            # duplicate of this note of the database
    of_origin: str | None = None          # duplicate of this earlier entry of the same run
    plan: Plan | None = None
    text: str = ""                        # a short snippet, only for ``new`` entries (never of refused ones)
    group: str = ""                       # the part of the source the entry belongs to (see ReadResult.groups)

    def as_dict(self) -> dict:
        d = {"n": self.n, "origin": self.origin, "outcome": self.outcome, "reason": self.reason, "group": self.group}
        if self.note_id is not None:
            d["note_id"] = self.note_id
        if self.of_origin is not None:
            d["of_origin"] = self.of_origin
        if self.plan is not None:
            d["kind"] = self.plan.kind
            d["text"] = self.text
        return d


def _trust(value) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        raise ValueError("not a number")
    t = float(value)
    if not 0.0 <= t <= 1.0:
        raise ValueError("outside 0 to 1")
    return t


def plan_entry(entry: Entry, reader: str) -> Plan:
    """The store's own rules applied to one entry, without a database: returns what would be written, or raises
    RefusedWrite / ValueError (the same exceptions, with the same words, as DecisionStore.add). Metadata that cannot be
    used (an unreadable or inconsistent date, a kind Hermind does not know, a trust outside 0..1) never blocks the text:
    it is dropped and named in ``Plan.notes``."""
    keywords = entry.keywords if isinstance(entry.keywords, str) else " ".join(str(k) for k in entry.keywords)
    content, keywords = check_text(entry.text, keywords)
    notes: list[str] = []
    created = None
    if entry.created not in (None, ""):
        try:
            created = parse_valid(entry.created)
        except ValueError:
            notes.append(f"created dropped: {entry.created!r} is not a date Hermind can read")
    dates, bad = {}, []
    for name, raw in (("valid_from", entry.valid_from), ("valid_to", entry.valid_to)):
        try:
            dates[name] = parse_valid(raw)
        except ValueError:
            dates[name] = None
            bad.append(name)
    if bad:
        notes.append(f"{' and '.join(bad)} dropped: not a date Hermind can read")
    elif dates["valid_from"] is not None and dates["valid_to"] is not None and dates["valid_to"] <= dates["valid_from"]:
        dates = {"valid_from": None, "valid_to": None}
        notes.append("valid_from and valid_to dropped: valid_to is not after valid_from")
    kind = (entry.kind or "").strip().lower()
    if kind and kind not in KINDS:
        notes.append(f"kind dropped: {kind!r} is not a kind Hermind knows; chosen from the text")
        kind = ""
    kind = kind or classify_kind(content)
    trust = None
    if entry.trust not in (None, ""):
        try:
            trust = _trust(entry.trust)
        except (ValueError, TypeError) as exc:
            why = "outside 0 to 1" if "outside" in str(exc) else "not a number"
            notes.append(f"trust dropped: {entry.trust!r} is {why}; a number from 0 to 1 is needed")
    return Plan(content=content, keywords=" ".join(str(keywords).split()), kind=kind, created_at=created,
                tags=[str(t) for t in entry.tags], pinned=bool(entry.pinned),
                source_session=entry.source_session or f"import:{reader}", valid_from=dates["valid_from"],
                valid_to=dates["valid_to"], review="pending" if entry.pending else "active", trust=trust,
                scope=_clean_scope(entry.scope) if entry.scope else None, notes=notes, source_id=entry.source_id,
                after=entry.after, entities=None if entry.entities is None else tuple(entry.entities),
                links=tuple(entry.links))


def _snippet(text: str, width: int = 70) -> str:
    t = " ".join(text.split())
    return t if len(t) <= width else t[:width - 1] + "…"


def decide(entries: Iterable[Entry], view, *, reader: str, scope: str = DEFAULT_SCOPE,
           exclude: Iterable[str] = ()) -> list[Decision]:
    """THE decision function: one Decision per entry, in order. Preview and apply both call it; apply only adds the
    writes. Reads the database through *view* and never writes.

    ``--exclude`` leaves entries out as if the source did not have them: the other entries are decided without them.
    Excluding a group therefore changes no other entry, with one exception that is the point of excluding: an entry
    whose text equals an excluded entry's is no longer a duplicate of it, so it comes in (leave out a memory list and
    keep the memory writes, and a write that repeats a list item is imported)."""
    entries = list(entries)
    patterns = [p for p in exclude if p]
    hits = [next((p for p in patterns if fnmatch.fnmatchcase(e.origin, p)), None) for e in entries]
    kept = [i for i, h in enumerate(hits) if h is None]
    decided = iter(_decide_all([entries[i] for i in kept], view, reader=reader, scope=scope))
    out: list[Decision] = []
    for i, (e, hit) in enumerate(zip(entries, hits)):
        if hit is not None:
            out.append(Decision(n=i + 1, origin=e.origin, outcome="excluded", reason=f"matches --exclude {hit}"))
        else:
            d = next(decided)
            d.n = i + 1                                       # the position in the source, excluded entries counted
            out.append(d)
    _resolve_versions(entries, out, view, reader)
    return out


def _resolve_versions(entries: list[Entry], decisions: list[Decision], view, reader: str) -> None:
    """An older version is judged only through its live note: it is written (outcome ``version``) when that note is
    written as new, and otherwise it is ``history`` with a reason that says which note it belongs to and why that note
    is not new. It is never a duplicate of its own live note's text. (Its own text still goes through the store's rules.)"""
    live = {}
    for e, d in zip(entries, decisions):
        if e.source_id is not None and e.version_of is None and not e.history and e.status not in HISTORY_STATUSES:
            live.setdefault(e.source_id, (e, d))
    for e, d in zip(entries, decisions):
        if e.version_of is None or d.outcome == "excluded":
            continue
        if not (e.text or "").strip():
            d.outcome, d.reason = "empty", "no text"
            continue
        if e.version_of not in live:
            d.outcome, d.reason = "history", (f"an older version of note #{e.version_of} of the file, which is not a live "
                                              f"note in it")
            continue
        le, ld = live[e.version_of]
        if ld.outcome != "new":
            d.outcome = "history"
            if ld.outcome == "duplicate":
                of = f"note #{ld.note_id}" if ld.note_id is not None else f"{ld.of_origin} earlier in this import"
                d.reason = f"an older version of {le.origin}, which is a duplicate of {of}"
            elif ld.outcome == "refused":
                d.reason = f"an older version of {le.origin}, which was refused ({ld.reason})"
            elif ld.outcome == "excluded":
                d.reason = f"an older version of {le.origin}, which is excluded ({ld.reason})"
            else:
                d.reason = f"an older version of {le.origin}, which was not imported ({ld.outcome}: {ld.reason})"
            continue
        try:
            plan = plan_entry(e, reader)
        except RefusedWrite as exc:
            d.outcome, d.reason = "refused", str(exc)
            continue
        except ValueError as exc:
            d.outcome, d.reason = ("empty", "no text") if str(exc) == "content is empty" else ("refused", f"refused: {exc}")
            continue
        if view.forgotten(_digest(plan.content)):
            d.outcome, d.reason = "refused", FORGOTTEN_REFUSAL
            continue
        plan.scope = _clean_scope(e.scope) if e.scope else ld.plan.scope
        plan.version = {"of": e.version_of, "status": e.status if e.status in HISTORY_STATUSES else "superseded",
                        "rel": e.version_rel or "version_of", "origin": le.origin}
        d.outcome, d.plan, d.text, d.reason = "version", plan, _snippet(plan.content), "; ".join(plan.notes)


def _decide_all(entries: list[Entry], view, *, reader: str, scope: str = DEFAULT_SCOPE) -> list[Decision]:
    scope = _clean_scope(scope)
    seen: dict[tuple, str] = {}            # (digest, scope) -> origin of the first ``new`` entry with that text
    out: list[Decision] = []
    default_scope = scope
    for n, e in enumerate(entries, 1):
        d = Decision(n=n, origin=e.origin, outcome="new")
        out.append(d)
        if e.version_of is not None:                              # judged through its live note: see _resolve_versions
            continue
        if e.unreadable:
            d.outcome, d.reason = "unreadable", e.unreadable
            continue
        body = e.body if e.body is not None else e.text
        if not (body or "").strip():
            d.outcome, d.reason = "empty", "no text"
            continue
        if e.owner_id is not None:
            if e.owner_relation == "same":
                d.outcome, d.note_id, d.reason = "duplicate", e.owner_id, f"same as #{e.owner_id}"
            else:
                d.outcome, d.note_id = "edited", e.owner_id
                d.reason = (f"edited copy of #{e.owner_id}{(' (' + e.owner_detail + ')') if e.owner_detail else ''}; "
                            f"not applied ({EDIT_SYNC_HINT})")
            continue
        if e.history:
            d.outcome, d.reason = "history", e.history
            continue
        if e.status in HISTORY_STATUSES:
            d.outcome, d.reason = "history", f"an older version kept for history (status: {e.status}); not imported as a live note"
            continue
        scope = _clean_scope(e.scope) if e.scope else default_scope
        if e.body is not None:                                   # three rules judge the body alone (a subject never lets in what it would not)
            try:
                cleaned, _ = check_text(e.body, "", screen=False)
            except RefusedWrite as exc:                          # (a RefusedWrite is a ValueError: ask for it first)
                if "<private>" in str(exc):
                    d.outcome, d.reason = "refused", str(exc)
                    continue
            except ValueError:
                d.outcome, d.reason = "empty", "no text"
                continue
            else:
                if view.forgotten(_digest(cleaned)):
                    d.outcome, d.reason = "refused", FORGOTTEN_REFUSAL
                    continue
        try:
            plan = plan_entry(e, reader)
        except RefusedWrite as exc:
            d.outcome, d.reason = "refused", str(exc)
            continue
        except ValueError as exc:
            if str(exc) == "content is empty":
                d.outcome, d.reason = "empty", "no text"
            else:
                d.outcome, d.reason = "refused", f"refused: {exc}"
            continue
        digest = _digest(plan.content)
        if view.forgotten(digest):
            d.outcome, d.reason = "refused", FORGOTTEN_REFUSAL
            continue
        row = view.find(digest, scope)
        if row is not None:
            d.outcome, d.note_id = "duplicate", row["id"]
            d.reason = f"same text as note #{row['id']}"
            if row["status"] != "active":
                d.reason += f" (not live: {row['status']})"
            elif row["review"] == "pending":
                d.reason += " (waiting in the review queue)"
            continue
        if (digest, scope) in seen:
            d.outcome, d.of_origin = "duplicate", seen[(digest, scope)]
            d.reason = f"same text as {seen[(digest, scope)]} earlier in this import"
            continue
        seen[(digest, scope)] = e.origin
        plan.scope = scope
        d.plan, d.text, d.reason = plan, _snippet(plan.content), "; ".join(plan.notes)
    return out


def count(decisions: Iterable[Decision]) -> dict:
    c = {k: 0 for k in OUTCOMES}
    for d in decisions:
        c[d.outcome] += 1
    return c


def assign_groups(entries: Iterable[Entry], decisions: list[Decision], groups: list[tuple[str, str]]) -> list[dict]:
    """Sort the entries into the reader's source groups (the first pattern that matches an origin wins; what no pattern
    matches is ``other``) and return ``[{name, exclude, counts, total}]``. Call it again after an apply: the counts then
    follow the final outcomes."""
    names = [g[0] for g in groups]
    for e, d in zip(entries, decisions):
        d.group = next((n for n, pat in groups if fnmatch.fnmatchcase(e.origin, pat)), "other")
    out = []
    for name, pat in groups:
        out.append(_group_row(name, pat, [d for d in decisions if d.group == name]))
    rest = [d for d in decisions if d.group == "other" and "other" not in names]
    if rest:
        out.append(_group_row("other", None, rest))
    return out


def _group_row(name: str, pattern: str | None, ds: list[Decision]) -> dict:
    return {"name": name, "exclude": pattern, "counts": count(ds), "total": len(ds)}


# -- the batch record and applying ------------------------------------------------------------------------------

_TABLES = """
CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    reader TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL,
    counts TEXT NOT NULL DEFAULT '{}',
    complete INTEGER NOT NULL DEFAULT 0,
    reverted_at REAL
);
CREATE TABLE IF NOT EXISTS import_batch_notes (
    batch_id TEXT NOT NULL,
    note_id INTEGER NOT NULL,
    origin TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    proof_count INTEGER NOT NULL DEFAULT 1,
    removed_at REAL,
    PRIMARY KEY (batch_id, note_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS import_batch_notes_note ON import_batch_notes(note_id);
CREATE TABLE IF NOT EXISTS import_batch_entities (
    batch_id TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, entity_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS import_batch_aliases (
    batch_id TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    PRIMARY KEY (batch_id, entity_id)
) WITHOUT ROWID;
"""

_STATE_COLS = ("keywords", "tags", "kind", "pinned", "review", "valid_from", "valid_to")


def ensure_tables(conn) -> None:
    """Create the batch tables (additive; only an applied import calls this)."""
    for stmt in _TABLES.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def new_batch_id(now: float | None = None) -> str:
    """Short and sortable: UTC date and time, milliseconds, one random hex digit pair (20261111-153012-482a1)."""
    t = time.time() if now is None else now
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(t)) + f"-{int(t % 1 * 1000):03d}" + secrets.token_hex(1)


KEEP_IMPORT_BACKUPS = 5          # the newest copies kept per database (import and revert share them)


def make_backup(db: Path, key: str | None) -> dict:
    """The backup that comes before the first write (see db_backup.py). Returns {backup, backup_removed, backup_note};
    a copy that cannot be made stops the run before anything is written."""
    try:
        b = backup_database(db, prefix="bak-import", keep=KEEP_IMPORT_BACKUPS, key=key)
    except BackupError as exc:
        raise ImportFailed(f"backup failed, nothing was written: {exc}") from exc
    if b is None:
        return {"backup": None, "backup_removed": 0, "backup_note": "no backup needed: the database did not exist yet"}
    return {"backup": str(b.path), "backup_removed": b.removed, "backup_note": ""}


def _open_store(db: Path, key: str | None):
    try:
        from .store import DecisionStore
    except ImportError:
        from store import DecisionStore
    return DecisionStore(str(db), key=key)


def _state_of(row) -> dict:
    return {k: row[k] for k in _STATE_COLS}


def apply_import(db: Path | str, reader: str, source: str, decisions: list[Decision], *, backup: bool = True,
                 chunk: int = CHUNK, key: str | None = None, now: float | None = None) -> dict:
    """Write the ``new`` decisions, then the ``version`` ones (older versions of notes written in this run), as one
    batch. Backup first (unless backup=False), then the batch record, then the notes in chunks of *chunk* (one commit
    each); the record is marked complete at the end, so a run that dies partway shows up as incomplete and can be
    reverted. Returns {batch, created, backup, changed}; on an exception the batch id is attached to it as
    ``exc.batch_id`` (the work already committed stays recorded)."""
    db = Path(db).expanduser()
    todo = [d for d in decisions if d.outcome == "new"] + [d for d in decisions if d.outcome == "version"]
    res: dict = {"batch": None, "created": 0, "backup": None, "backup_removed": 0, "changed": []}
    if not todo:
        return res
    if backup:
        res.update(make_backup(db, key))
    store = _open_store(db, key)
    batch_id = None
    try:
        t = time.time() if now is None else now
        with store._lock, store.conn:
            ensure_tables(store.conn)
            batch_id = new_batch_id(t)
            while store.conn.execute("SELECT 1 FROM import_batches WHERE id=?", (batch_id,)).fetchone():
                batch_id = new_batch_id(t)
            store.conn.execute("INSERT INTO import_batches(id, reader, source, created_at, counts, complete) "
                               "VALUES (?,?,?,?,?,0)", (batch_id, reader, source, t, json.dumps(count(decisions))))
        res["batch"] = batch_id
        tag = f"import-{batch_id}"
        written: dict[int, int] = {}                               # the source's id of a written note -> its new id
        for lo in range(0, len(todo), max(1, chunk)):
            with store._lock, store.conn:                       # one commit per chunk
                if not store.conn.in_transaction:
                    store.conn.execute("BEGIN IMMEDIATE")        # from here no other writer can slip in between two notes:
                                                                 # the entities that appear while a note is added are its own
                for d in todo[lo:lo + max(1, chunk)]:
                    p = d.plan
                    top = store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM entities").fetchone()[0]
                    if d.outcome == "version":
                        live = written.get(p.version["of"])
                        if live is None:                           # its note did not get written after all (changed meanwhile)
                            res["changed"].append({"origin": d.origin, "was": "version", "now": "history",
                                                   "reason": f"its note {p.version['origin']} was not written"})
                            d.outcome, d.plan = "history", None
                            d.reason = f"an older version of {p.version['origin']}, which was not written"
                            continue
                        nid = store.add_older_version(
                            live, p.content, kind=p.kind, keywords=p.keywords, created_at=p.created_at if p.created_at is not None else t,
                            status=p.version["status"], rel=p.version["rel"], valid_from=p.valid_from, valid_to=p.valid_to,
                            trust=p.trust if p.trust is not None else TRUST_DEFAULT, scope=p.scope, tags=[*p.tags, tag],
                            review=p.review, pinned=p.pinned, source_session=p.source_session,
                            entities=[n for n, _ in p.entities or ()], now=t, commit=False,
                            exact_entities=p.entities is not None)
                    else:
                        try:
                            nid, created = store.add(
                                p.content, kind=p.kind, keywords=p.keywords, created_at=p.created_at,
                                source_session=p.source_session, pinned=p.pinned, tags=[*p.tags, tag], review=p.review,
                                valid_from=p.valid_from, valid_to=p.valid_to, never_expires=True, touch_existing=False,
                                scope=p.scope, entities=[n for n, _ in p.entities or ()], commit=False,
                                exact_entities=p.entities is not None)
                        except (RefusedWrite, ValueError) as exc:      # changed since the decision (another writer)
                            res["changed"].append({"origin": d.origin, "was": "new", "now": "refused", "reason": str(exc)})
                            d.outcome, d.reason, d.plan = "refused", str(exc), None
                            continue
                        if not created:                                # the same text appeared meanwhile: left untouched
                            res["changed"].append({"origin": d.origin, "was": "new", "now": "duplicate",
                                                   "reason": f"same text as note #{nid}"})
                            d.outcome, d.note_id, d.reason, d.plan = "duplicate", nid, f"same text as note #{nid}", None
                            continue
                        if p.trust is not None:
                            store.conn.execute("UPDATE notes SET trust=? WHERE id=?", (p.trust, nid))
                        # the idle clock of an imported note starts now: it keeps the source's date as created_at, but nobody
                        # could have recalled it before this import, so it must not start out faded (see _idle_factor).
                        # access_count stays 0, so the hotness signal does not see it.
                        store.conn.execute("UPDATE notes SET last_access=? WHERE id=?", (t, nid))
                    if p.after:                                    # what the source's note had learned (before the state is recorded)
                        store.restore_fields(nid, commit=False, **p.after)
                    row = store.conn.execute("SELECT content_hash, proof_count, " + ", ".join(_STATE_COLS) +
                                             " FROM notes WHERE id=?", (nid,)).fetchone()
                    # what the revert needs to take back what this note taught the entity index (see apply_revert)
                    state = _state_of(row)
                    state["entities"] = [r[0] for r in store.conn.execute(
                        "SELECT entity_id FROM note_entities WHERE note_id=? ORDER BY entity_id", (nid,))]
                    if d.outcome == "version":
                        state["version"] = {"of": live}
                    store.conn.executemany(
                        "INSERT OR IGNORE INTO import_batch_entities(batch_id, entity_id, aliases) VALUES (?,?,?)",
                        [(batch_id, r[0], r[1]) for r in store.conn.execute(
                            "SELECT id, aliases FROM entities WHERE id > ?", (top,))])
                    store.conn.execute(
                        "INSERT INTO import_batch_notes(batch_id, note_id, origin, content_hash, state, proof_count) "
                        "VALUES (?,?,?,?,?,?)",
                        (batch_id, nid, d.origin, row["content_hash"], json.dumps(state), row["proof_count"]))
                    d.note_id = nid
                    if p.source_id is not None:
                        written.setdefault(p.source_id, nid)
                    res["created"] += 1
        _finish_hermind_parts(store, batch_id, decisions, written, t)
        with store._lock, store.conn:
            store.conn.execute("UPDATE import_batches SET counts=?, complete=1 WHERE id=?",
                               (json.dumps(count(decisions)), batch_id))
        return res
    except BaseException as exc:
        exc.batch_id = batch_id                                    # type: ignore[attr-defined]
        raise
    finally:
        store.close()


def _finish_hermind_parts(store, batch_id: str, decisions: list[Decision], written: dict[int, int], t: float) -> None:
    """After the notes of a batch are written: recreate the links between two notes of this batch (with their rel), add
    the entity aliases the source gives to the entities of the batch's notes, and record both for the revert."""
    made = [d for d in decisions if d.outcome in ("new", "version") and d.plan is not None and d.note_id is not None]
    with store._lock, store.conn:
        for d in made:                                              # links between two notes this import wrote
            for to, rel in d.plan.links:
                if to in written:
                    store.conn.execute("INSERT OR IGNORE INTO note_links(src, dst, rel, created_at) VALUES (?,?,?,?)",
                                       (d.note_id, written[to], rel, t))
            # An older version whose own link points at a middle note (merged into a note that was merged in turn) got a
            # link to its canonical note when it was written; the source has no such link, so take it out again.
            v = d.plan.version if d.outcome == "version" else None
            if v and v["of"] in written and (v["of"], v["rel"]) not in d.plan.links and any(
                    r in VERSION_RELS and to != v["of"] and to in written for to, r in d.plan.links):
                store.conn.execute("DELETE FROM note_links WHERE src=? AND dst=? AND rel=?",
                                   (d.note_id, written[v["of"]], v["rel"]))
        aliases: dict[str, tuple[str, list[str]]] = {}              # entity key -> (name, aliases the source gives it)
        for d in made:
            for name, als in d.plan.entities or ():
                k = _entity_key(name)
                have = aliases.setdefault(k, (name, []))[1]
                have.extend(a for a in als if a and a not in have)
        for k, (name, als) in aliases.items():
            row = store.conn.execute("SELECT id, aliases FROM entities WHERE key=?", (k,)).fetchone() if als else None
            if row is None:                                         # only the entities the batch's notes really have
                continue
            store._get_or_create_entity(k, name, als)
            after = store.conn.execute("SELECT aliases FROM entities WHERE id=?", (row[0],)).fetchone()[0]
            if after != row[1]:
                store.conn.execute("INSERT OR REPLACE INTO import_batch_aliases(batch_id, entity_id, before, after) "
                                   "VALUES (?,?,?,?)", (batch_id, row[0], row[1], after))
        for d in made:                                              # the links each note has now are what the batch wrote
            rels = sorted({r[0] for r in store.conn.execute("SELECT rel FROM note_links WHERE src=? OR dst=?",
                                                            (d.note_id, d.note_id))})
            if rels:
                rec = store.conn.execute("SELECT state FROM import_batch_notes WHERE batch_id=? AND note_id=?",
                                         (batch_id, d.note_id)).fetchone()
                state = json.loads(rec[0] or "{}")
                state["links"] = rels
                store.conn.execute("UPDATE import_batch_notes SET state=? WHERE batch_id=? AND note_id=?",
                                   (json.dumps(state), batch_id, d.note_id))


# -- reverting ---------------------------------------------------------------------------------------------------

def check_note(rec: dict, now: dict | None) -> str | None:
    """Why a note of a batch must stay (None when it is still exactly what the batch wrote). *now* is
    View.note_state() (None = the note is gone)."""
    if now is None:
        return None
    if now["status"] != "active":
        return f"{now['status']} after the import"
    if now["content_hash"] != rec["content_hash"]:
        return "edited after the import (the text changed)"
    was = rec["state"]
    changed = [c for c in _STATE_COLS if was.get(c) != now[c]]
    if "pinned" in changed and now["pinned"]:
        return "pinned after the import"
    if changed:
        return "edited after the import (" + ", ".join(c.replace("_", " ") for c in changed) + " changed)"
    new_links = [r for r in now["links"] if r not in was.get("links", [])]      # links the batch itself wrote are not new
    if new_links:
        rel = ", ".join(new_links)
        return f"linked to other notes after the import ({rel}): merged or superseded"
    if now["proof_count"] != rec["proof_count"]:
        return f"saved again after the import (proof count {now['proof_count']})"
    return None


def plan_revert(view, batch_id: str) -> dict:
    """What reverting *batch_id* would do: {remove: [...], keep: [{..., reason}], gone: [...]}; read-only."""
    remove, keep, gone, versions = [], [], [], []
    stays: dict[int, str] = {}                 # a note of the batch that stays -> why
    for rec in view.batch_notes(batch_id):
        if rec["state"].get("version"):        # an older version goes with its note, see below
            versions.append(rec)
            continue
        now = view.note_state(rec["note_id"])
        item = {"note_id": rec["note_id"], "origin": rec["origin"]}
        if now is None:
            gone.append(item)
            continue
        why = check_note(rec, now)
        if why:
            stays[rec["note_id"]] = why
        (keep if why else remove).append({**item, "reason": why} if why else item)
    for rec in versions:                       # an older version is removed with the note it belongs to, and stays with it
        of = rec["state"]["version"]["of"]
        now = view.note_state(rec["note_id"])
        item = {"note_id": rec["note_id"], "origin": rec["origin"]}
        if now is None:
            gone.append(item)
        elif now["status"] not in HISTORY_STATUSES or now["canonical_id"] != of:
            keep.append({**item, "reason": f"changed after the import: it is no longer an older version of note #{of}"})
        elif of in stays:
            keep.append({**item, "reason": f"its note #{of} stays ({stays[of]})"})
        else:
            remove.append(item)
    return {"remove": remove, "keep": keep, "gone": gone}


def _take_back_links(conn, entity_lists) -> None:
    """Take off the entity_links weight that the removed notes added when they were written: one for every pair of the
    entities a note was linked to (recorded at apply time). A link whose weight falls to nothing is removed."""
    pairs: dict[tuple[int, int], int] = {}
    for ids in entity_lists:
        ids = sorted(set(ids or ()))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                pairs[(a, b)] = pairs.get((a, b), 0) + 1
    conn.executemany("UPDATE entity_links SET weight=weight-? WHERE a=? AND b=?", [(n, a, b) for (a, b), n in pairs.items()])
    conn.executemany("DELETE FROM entity_links WHERE a=? AND b=? AND weight <= 0.000001", list(pairs))


def _remove_unused_entities(conn, batch_id: str) -> int:
    """Remove the entities the batch created once no note uses them and nobody has given them an alias since (their
    aliases are still what they were at creation, i.e. only the synonym table's); returns how many went."""
    gone = 0
    for eid, aliases in conn.execute("SELECT entity_id, aliases FROM import_batch_entities WHERE batch_id=?",
                                     (batch_id,)).fetchall():
        if conn.execute("DELETE FROM entities WHERE id=? AND aliases=? AND NOT EXISTS "
                        "(SELECT 1 FROM note_entities WHERE entity_id=?)", (eid, aliases, eid)).rowcount:
            conn.execute("DELETE FROM entity_links WHERE a=? OR b=?", (eid, eid))
            gone += 1
    return gone


def apply_revert(db: Path | str, batch_id: str, *, backup: bool = True, key: str | None = None,
                 chunk: int = CHUNK, now: float | None = None) -> dict:
    """Remove what the batch created (everything forget() does except the tombstone, so the same text can be imported
    again). Notes changed since the import stay. Returns {removed, kept, gone, backup}."""
    db = Path(db).expanduser()
    res: dict = {"backup": None, "backup_removed": 0, "entities_removed": 0}
    if backup:
        res.update(make_backup(db, key))
    store = _open_store(db, key)
    try:
        t = time.time() if now is None else now
        with store._lock:
            ensure_tables(store.conn)       # a no-op for a database that has batches; keeps the queries below simple
        view = View(store.conn)
        plan = plan_revert(view, batch_id)
        recs = view.batch_notes(batch_id)
        older = {r["note_id"] for r in recs if r["state"].get("version")}
        ids = [i["note_id"] for i in plan["remove"]]
        # the older versions go first: deleting a note turns every row whose canonical_id points at it back into an
        # active note (see DecisionStore._delete_ids), so a version must not be left for that
        ids = [i for i in ids if i in older] + [i for i in ids if i not in older]
        recorded = {r["note_id"]: r["state"].get("entities") for r in recs}
        for lo in range(0, len(ids), max(1, chunk)):
            part = ids[lo:lo + max(1, chunk)]
            with store._lock, store.conn:
                store._delete_ids(part)
                _take_back_links(store.conn, [recorded.get(i) for i in part])
                store.conn.executemany("UPDATE import_batch_notes SET removed_at=? WHERE batch_id=? AND note_id=?",
                                       [(t, batch_id, i) for i in part])
        with store._lock, store.conn:
            for eid, before, after in store.conn.execute(          # the aliases the batch added are taken back (unless
                    "SELECT entity_id, before, after FROM import_batch_aliases WHERE batch_id=?",   # somebody changed them since)
                    (batch_id,)).fetchall():
                store.conn.execute("UPDATE entities SET aliases=? WHERE id=? AND aliases=?", (before, eid, after))
            res["entities_removed"] = _remove_unused_entities(store.conn, batch_id)
            store.conn.executemany("UPDATE import_batch_notes SET removed_at=? WHERE batch_id=? AND note_id=?",
                                   [(t, batch_id, i["note_id"]) for i in plan["gone"]])
            store.conn.execute("UPDATE import_batches SET reverted_at=? WHERE id=?", (t, batch_id))
        res.update(removed=plan["remove"], kept=plan["keep"], gone=plan["gone"])
        return res
    finally:
        store.close()


# -- the command-level entry points (hermind_cli.py is a thin wrapper around these) ------------------------------------

class ImportFailed(Exception):
    """A real error (unreadable source, unknown reader, ambiguous source): the command exits non-zero."""


def run_import(source: str, db: Path | str, *, reader: str | None = None, apply: bool = False,
               exclude: Iterable[str] = (), backup: bool = True, key: str | None = None,
               chunk: int = CHUNK, options: dict | None = None) -> dict:
    """Preview (default) or apply an import; returns the JSON-ready result dict (ok / error on failure). *options* are
    reader options by name (``encoding``, ``ignore_file_names``); a value that is None / False means "not given", and
    one the chosen reader does not take is an error."""
    src = Path(source).expanduser()
    if not src.exists():
        raise ImportFailed(f"{source}: no such file or folder")
    available = readers()
    detected = False
    if reader is None:
        reader, ties = detect_reader(src)
        if reader is None:
            if ties:
                raise ImportFailed(f"{source} looks like more than one kind of source ({', '.join(ties)}): "
                                   f"say which with --from NAME")
            raise ImportFailed(f"{source}: no reader recognises this source; --readers lists what is supported "
                               f"(choose one with --from NAME)")
        detected = True
    if reader not in available:
        raise ImportFailed(f"unknown reader {reader!r}; --readers lists them ({', '.join(sorted(available))})")
    given = {k: v for k, v in (options or {}).items() if v not in (None, False)}
    unknown = sorted(set(given) - set(available[reader].options))
    if unknown:
        flags = ", ".join("--" + k.replace("_", "-") for k in unknown)
        raise ImportFailed(f"{flags} {'is' if len(unknown) == 1 else 'are'} not used by the {reader} reader")
    if "encoding" in given:
        try:
            codecs.lookup(given["encoding"])
        except LookupError:
            raise ImportFailed(f"unknown encoding {given['encoding']!r} for --encoding (examples: big5, gb18030, cp1252, shift_jis)") from None
    view = open_view(db, key)
    try:
        try:
            read = available[reader].read(src, view, **given)
        except OSError as exc:
            raise ImportFailed(f"cannot read {source}: {exc.strerror or exc}") from exc
        decisions = decide(read.entries, view, reader=reader, exclude=exclude)
        groups = assign_groups(read.entries, decisions, read.groups)
    finally:
        view.close()
    base_remarks = list(read.remarks)
    out = {"ok": True, "mode": "apply" if apply else "preview", "database": str(Path(db).expanduser()),
           "source": str(source), "reader": reader, "reader_detected": detected, "counts": count(decisions),
           "total": len(decisions), "remarks": _remarks(read, base_remarks, decisions), "groups": groups, "batch": None, "created": 0,
           "backup": None, "backup_removed": 0, "changed_since_preview": []}
    if apply:
        try:
            r = apply_import(db, reader, str(source), decisions, backup=backup, chunk=chunk, key=key)
        except Exception as exc:
            out.update(ok=False, error=f"import stopped: {exc}", batch=getattr(exc, "batch_id", None))
            out["counts"], out["entries"] = count(decisions), [d.as_dict() for d in decisions]
            out["groups"] = assign_groups(read.entries, decisions, read.groups)
            if out["batch"]:
                out["error"] += (f" -- batch {out['batch']} is incomplete; `hermind import --revert {out['batch']} "
                                 f"--apply` removes what it wrote")
            return out
        out.update(batch=r["batch"], created=r["created"], backup=r["backup"], changed_since_preview=r["changed"],
                   backup_note=r.get("backup_note", ""), backup_removed=r["backup_removed"])
        out["counts"] = count(decisions)
        out["groups"] = assign_groups(read.entries, decisions, read.groups)
        out["remarks"] = _remarks(read, base_remarks, decisions)
    out["entries"] = [d.as_dict() for d in decisions]
    return out


def _remarks(read: ReadResult, base: list[str], decisions: list[Decision]) -> list[str]:
    """The reader's remarks, plus the ones that depend on what was decided (a reader's ``summarise``)."""
    return base + (list(read.summarise(read.entries, decisions)) if read.summarise else [])


def run_list(db: Path | str, key: str | None = None) -> dict:
    view = open_view(db, key)
    try:
        return {"ok": True, "database": str(Path(db).expanduser()), "batches": view.batches()}
    finally:
        view.close()


def run_revert(batch_id: str, db: Path | str, *, apply: bool = False, backup: bool = True,
               key: str | None = None) -> dict:
    view = open_view(db, key)
    try:
        if not view.exists or not view.batch_exists(batch_id):
            raise ImportFailed(f"no import batch {batch_id!r} in {db}; `hermind import --list` shows the batches")
        plan = plan_revert(view, batch_id)
    finally:
        view.close()
    out = {"ok": True, "mode": "apply" if apply else "preview", "database": str(Path(db).expanduser()),
           "batch": batch_id, "backup": None, **plan}
    if apply:
        r = apply_revert(db, batch_id, backup=backup, key=key)
        out.update(backup=r["backup"], backup_removed=r["backup_removed"], remove=r["removed"], keep=r["kept"],
                   gone=r["gone"], entities_removed=r["entities_removed"])
    return out


def reader_list() -> list[dict]:
    return [{"name": r.name, "accepts": r.summary} for r in readers().values()]

