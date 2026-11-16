"""Project identity: which working directory a note was written in, and whether two directories are the same project.

A project is a normalised directory path (`normalize`). It is internal: it is stored in `notes.project`, used to rank
recall, and never shown in a tool result, an MCP result or an export (a path is private to the writer's machine).

`None` means "no project / unknown": no working directory, a filesystem root, the user's home directory, or an umbrella
folder that holds two or more projects already stored in the database (`project_for`). A note with no project and a
session with no project are neutral: they never count as a mismatch.

The second half of the module is the recall penalty (`ProjectRule`): a note written in another project is ranked lower in
the automatic recall, never hidden. It acts on the kinds in PROJECT_PENALTY_KINDS only, never on a pinned note or one that
was used or confirmed, and never when it would push the note's combined multiplier under PROJECT_FLOOR.

Standard library only. `pm` is the path module (os.path by default; ntpath in tests of Windows spelling).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

PROJECT_PENALTY = 0.5                          # score factor of a note from another project (1.0 = off)
PROJECT_PENALTY_KINDS = ("chat", "summary")    # the kinds it applies to; technical facts and decisions stay at full rank
PROJECT_FLOOR = 0.25                           # never demote a note whose combined multiplier would end below this
_MAX_CACHE = 4096   # distinct paths remembered per matcher; far more than the projects of one person


def normalize(cwd, *, pm=os.path) -> str | None:
    """`pm.normcase(pm.abspath(cwd))` without a trailing separator; None for no usable path (empty, not a string or path,
    a NUL byte) and for a filesystem root ("/", "C:\\", a UNC share root), which names no project."""
    if isinstance(cwd, os.PathLike):
        cwd = os.fspath(cwd)
    if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
        return None
    try:
        path = pm.normcase(pm.abspath(cwd))
    except (ValueError, OSError):
        return None
    if pm.dirname(path) == path:          # "/", "C:\\", a UNC share root: the only paths that are their own parent
        return None
    return path


def _related(a: str, b: str, pm) -> bool:
    """Equal, or one is a directory above the other, component by component (never a bare string prefix)."""
    if a == b:
        return True
    try:
        return pm.commonpath([a, b]) in (a, b)
    except ValueError:      # different drives, or one path absolute and the other not
        return False


class ProjectMatcher:
    """`matches(a, b)` for two normalised project paths. Strings that differ are compared once more by their real paths
    (symlinks, a case-insensitive disk), each real path computed once per distinct value."""

    def __init__(self, *, pm=os.path):
        self.pm = pm
        self._real: dict[str, str] = {}
        self._pairs: dict[tuple[str, str], bool] = {}

    def real(self, path: str) -> str:
        got = self._real.get(path)
        if got is None:
            try:
                got = self.pm.normcase(self.pm.realpath(path))
            except (ValueError, OSError):
                got = path
            if len(self._real) >= _MAX_CACHE:
                self._real.clear()
            got = self._real[path] = got
        return got

    def matches(self, a: str | None, b: str | None) -> bool:
        """True when a or b is missing (unknown matches everything), when they are equal or one lies inside the other,
        or when their real paths do. False only for two known, unrelated projects."""
        if not a or not b:
            return True
        if _related(a, b, self.pm):
            return True
        key = (a, b)
        hit = self._pairs.get(key)
        if hit is None:
            hit = _related(self.real(a), self.real(b), self.pm)
            if len(self._pairs) >= _MAX_CACHE:
                self._pairs.clear()
            self._pairs[key] = hit
        return hit


def project_for(cwd, known: Iterable[str] | Callable[[], Iterable[str]] = (), *, home=None, pm=os.path,
                matcher: ProjectMatcher | None = None, normalized: bool = False) -> str | None:
    """The project of a session that started in *cwd*: its normalised path, or None when it is neutral:
    no usable path, a filesystem root, the user's home directory (*home*, default Path.home()), or a strict ancestor of
    two or more distinct projects in *known* (the project values already stored; a function is called only when needed).
    An umbrella folder such as ~/work would otherwise tag every note of every project below it with the umbrella.
    *normalized* says *known* already holds `normalize`d paths (the import command normalises its whole set once)."""
    path = normalize(cwd, pm=pm)
    if path is None:
        return None
    matcher = matcher or ProjectMatcher(pm=pm)
    if home is None:
        try:
            home = Path.home()
        except (RuntimeError, KeyError, OSError):
            home = None
    home_path = normalize(home, pm=pm) if home is not None else None
    if home_path is not None and (path == home_path or matcher.real(path) == matcher.real(home_path)):
        return None
    stored = known() if callable(known) else known
    values = (stored or ()) if normalized else (normalize(k, pm=pm) for k in stored or ())
    below = {p for p in values if p and p != path and _is_below(p, path, pm)}
    return None if len(below) >= 2 else path


def _is_below(child: str, parent: str, pm) -> bool:
    try:
        return pm.commonpath([child, parent]) == parent
    except ValueError:
        return False


# -- the recall penalty ---------------------------------------------------------------------------------------

def parse_penalty(value, default: float = PROJECT_PENALTY) -> float:
    """A factor between 0 and 1 (a number or numeric text); *default* for anything else, including NaN."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else min(1.0, max(0.0, f))


def parse_kinds(value) -> frozenset[str]:
    """Kinds from a comma or space separated string or a list, lower-cased. None means the default; an empty string or list
    means none (the penalty is off)."""
    if value is None:
        value = PROJECT_PENALTY_KINDS
    items = value if isinstance(value, (list, tuple, set, frozenset)) else str(value).replace(",", " ").split()
    return frozenset(str(v).strip().lower() for v in items if str(v).strip())


class ProjectRule:
    """The demotion of notes written in another project, for one session.

    `raw(...)` is the factor the rule asks for (the penalty, or 1.0 when it does not apply). `factor(...)` adds the floor:
    *combined* is the product of the multipliers the ranking has already applied to that note's score, and the penalty is
    skipped when combined * penalty would fall under PROJECT_FLOOR, so stacked demotions can never bury a note."""

    __slots__ = ("project", "penalty", "kinds", "matcher", "trust_default")

    def __init__(self, project: str, penalty: float, kinds: frozenset[str], matcher: ProjectMatcher, trust_default: float):
        self.project, self.penalty, self.kinds = project, penalty, kinds
        self.matcher, self.trust_default = matcher, trust_default

    @classmethod
    def make(cls, project, penalty, kinds, matcher: ProjectMatcher, trust_default: float) -> "ProjectRule | None":
        """The rule, or None when it would change nothing: no current project, a factor of 1 or more, or no kinds."""
        if not project or penalty >= 1.0 or not kinds:
            return None
        return cls(project, penalty, frozenset(kinds), matcher, trust_default)

    def raw(self, note_project, kind, pinned, opened_count, proof_count, trust) -> float:
        """All of: the note has a project that does not match the current one; its kind is listed; it is not pinned; it was
        not used or confirmed (opened, proof_count >= 2, or trust above the default). Otherwise 1.0."""
        if not note_project or kind not in self.kinds or pinned:
            return 1.0
        if (opened_count or 0) > 0 or (proof_count or 1) >= 2 or (trust if trust is not None else self.trust_default) > self.trust_default:
            return 1.0
        return 1.0 if self.matcher.matches(self.project, note_project) else self.penalty

    def factor(self, note_project, kind, pinned, opened_count, proof_count, trust, combined: float = 1.0) -> float:
        f = self.raw(note_project, kind, pinned, opened_count, proof_count, trust)
        return f if f >= 1.0 or combined * f >= PROJECT_FLOOR else 1.0

    def factor_of(self, row, combined: float = 1.0) -> float:
        """`factor` for a row of `notes` (an sqlite3.Row, or any mapping with those columns)."""
        return self.factor(row["project"], row["kind"], row["pinned"], row["opened_count"], row["proof_count"],
                           row["trust"], combined)
