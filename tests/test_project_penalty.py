"""The recall penalty: notes written in another project rank lower in the automatic recall, never hidden."""
import json
import sys
from pathlib import Path

import pytest

from project import PROJECT_FLOOR, ProjectMatcher, ProjectRule, parse_kinds, parse_penalty
from store import KINDS, DecisionStore

HERE = "/srv/work/atlas"
OTHER = "/srv/work/borealis"
QUERY = "orbit scheduler"
FILLER = ["orbit scheduler retries failed jobs three times", "the scheduler runs jobs near orbit launch windows on weekdays",
          "orbit notes: scheduler logs rotate every day", "scheduler orbit dashboard shows queue depth per team",
          "orbit scheduler cron expressions use utc", "orbit scheduler alerts page the on call engineer"]


def make_store(tmp_path=None, *, kind="chat", first_project=OTHER, filler_project=HERE, filler_kind="chat", **kw):
    """Six filler notes (project HERE) and one strong note (the first) that is #1 without a project."""
    s = DecisionStore(":memory:", **kw)
    first, _ = s.add("orbit scheduler retry policy", keywords="orbit scheduler", kind=kind, project=first_project)
    others = [s.add(t, kind=filler_kind, project=filler_project)[0] for t in FILLER]
    return s, first, others


def ids(hits):
    return [h["id"] for h in hits]


def test_the_fixture_puts_the_other_projects_note_first_without_a_project():
    s, first, _ = make_store()
    assert ids(s.search(QUERY, limit=7))[0] == first
    assert ids(s.search(QUERY, limit=7, project=OTHER))[0] == first       # its own project: not demoted


# -- which notes -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_only_the_listed_kinds_are_demoted(kind):
    s, first, _ = make_store(kind=kind)
    order = ids(s.search(QUERY, limit=7, project=HERE))
    if kind in ("chat", "summary"):
        assert order[-1] == first
    else:
        assert order[0] == first


def test_the_kinds_come_from_the_configuration():
    s, first, _ = make_store(kind="tech", project_penalty_kinds="tech, state")
    assert ids(s.search(QUERY, limit=7, project=HERE))[-1] == first
    s, first, _ = make_store(kind="chat", project_penalty_kinds=["tech"])
    assert ids(s.search(QUERY, limit=7, project=HERE))[0] == first


def test_the_factor_comes_from_the_configuration():
    s, first, _ = make_store(project_penalty=0.9)
    with_project = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, project=HERE)}
    without = {h["id"]: h["score"] for h in s.search(QUERY, limit=7)}
    assert with_project[first] == pytest.approx(without[first] * 0.9, rel=1e-3)
    assert {i: v for i, v in with_project.items() if i != first} == {i: v for i, v in without.items() if i != first}


def test_the_default_factor_is_one_half():
    s, first, _ = make_store()
    a = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, project=HERE)}
    b = {h["id"]: h["score"] for h in s.search(QUERY, limit=7)}
    assert a[first] == pytest.approx(b[first] * 0.5, rel=1e-3)


def test_a_demoted_note_is_still_found():
    s, first, _ = make_store()
    assert first in ids(s.search(QUERY, limit=7, project=HERE))


@pytest.mark.parametrize("how", ["pinned", "opened", "proof", "trust"])
def test_used_confirmed_and_pinned_notes_are_never_demoted(how):
    s = DecisionStore(":memory:")
    kw = {"pinned": True} if how == "pinned" else {}
    first, _ = s.add("orbit scheduler retry policy", keywords="orbit scheduler", kind="chat", project=OTHER, **kw)
    for t in FILLER:
        s.add(t, kind="chat", project=HERE)
    with s.conn:
        if how == "opened":
            s.conn.execute("UPDATE notes SET opened_count=1, shown_count=2 WHERE id=?", (first,))
        elif how == "proof":
            s.conn.execute("UPDATE notes SET proof_count=2 WHERE id=?", (first,))
        elif how == "trust":
            s.set_fields(first, trust=0.8)
    s._ent_cache = None
    rule = s.project_rule(HERE)
    row = s.conn.execute("SELECT * FROM notes WHERE id=?", (first,)).fetchone()
    assert rule.raw(row["project"], row["kind"], row["pinned"], row["opened_count"], row["proof_count"], row["trust"]) == 1.0
    assert ids(s.search(QUERY, limit=7, project=HERE))[0] == first


def test_one_proof_default_trust_and_no_opens_are_not_confirmation():
    s, first, _ = make_store()
    row = s.conn.execute("SELECT * FROM notes WHERE id=?", (first,)).fetchone()
    assert (row["proof_count"], row["trust"], row["opened_count"], row["pinned"]) == (1, 0.5, 0, 0)
    assert s.project_rule(HERE).raw(row["project"], row["kind"], 0, 0, 1, 0.5) == 0.5


def test_a_note_without_a_project_is_never_demoted():
    s, first, _ = make_store(first_project=None)
    assert ids(s.search(QUERY, limit=7, project=HERE))[0] == first


@pytest.mark.parametrize("note_project", [HERE, HERE + "/services/api", "/srv/work"])
def test_a_note_of_the_same_or_a_nested_project_is_never_demoted(note_project):
    s, first, _ = make_store(first_project=note_project, filler_project=OTHER)
    assert ids(s.search(QUERY, limit=7, project=HERE))[0] == first


def test_a_name_that_only_shares_a_prefix_is_another_project():
    s, first, _ = make_store(first_project="/srv/work/atlas-old", filler_project=None)
    assert ids(s.search(QUERY, limit=7, project=HERE))[-1] == first


# -- before the cut ---------------------------------------------------------------------------------------------

def test_the_penalty_acts_before_the_limit_cut():
    s, first, _ = make_store(kind="chat")
    assert ids(s.search(QUERY, limit=3))[0] == first
    assert first not in ids(s.search(QUERY, limit=3, project=HERE))          # left the top 3 ...
    assert first in ids(s.search(QUERY, limit=7, project=HERE))              # ... and is still found further down
    s, first, _ = make_store(kind="tech")
    assert ids(s.search(QUERY, limit=3, project=HERE))[0] == first           # an unlisted kind in the same place stays


def test_search_multi_hands_the_project_to_every_query():
    s, first, _ = make_store()
    assert ids(s.search_multi([QUERY, "scheduler orbit"], limit=3))[0] == first
    assert first not in ids(s.search_multi([QUERY, "scheduler orbit"], limit=3, project=HERE))
    assert first not in ids(s.search_multi([QUERY], limit=3, project=HERE))   # one query takes the search() shortcut
    seen = []
    real = s.search
    s.search = lambda *a, **k: (seen.append(k.get("project", "absent")), real(*a, **k))[1]
    s.search_multi([QUERY, "scheduler orbit"], limit=3, project=HERE)
    s.search_multi([QUERY], limit=3, project=HERE)
    s.search_multi([QUERY, "scheduler orbit"], limit=3)
    assert seen == [HERE, HERE, HERE, None, None]


# -- no effect ---------------------------------------------------------------------------------------------------

def _run(s, **kw):
    return [(h["id"], h["score"]) for h in s.search(QUERY, limit=7, **kw)], \
           [(h["id"], h["score"]) for h in s.search_multi([QUERY, "scheduler orbit"], limit=7, **kw)]


def test_no_project_gives_the_old_ranking():
    s, _, _ = make_store()
    assert _run(s) == _run(s, project=None) == _run(s, project="")


def test_a_factor_of_one_or_no_kinds_give_the_old_ranking():
    s, _, _ = make_store(project_penalty=1.0)
    assert _run(s, project=HERE) == _run(s)
    assert s.project_rule(HERE) is None
    s, _, _ = make_store(project_penalty_kinds="")
    assert _run(s, project=HERE) == _run(s)
    assert s.project_rule(HERE) is None
    s, _, _ = make_store(project_penalty_kinds=[])
    assert _run(s, project=HERE) == _run(s)


def test_notes_of_no_project_or_the_same_project_give_the_old_ranking():
    s, _, _ = make_store(first_project=None, filler_project=None)
    assert _run(s, project=HERE) == _run(s)
    s, _, _ = make_store(first_project=HERE, filler_project=HERE)
    assert _run(s, project=HERE) == _run(s)


def test_interleave_does_not_apply_a_project():
    """The candidate-dedup ordering returns before the rescoring; the docstring says so and this holds it to that."""
    s, _, _ = make_store()
    base = ids(s.search(QUERY, limit=7, fusion="interleave"))
    assert ids(s.search(QUERY, limit=7, fusion="interleave", project=HERE)) == base
    s2, _, _ = make_store(project_penalty_kinds="chat")
    assert ids(s2.search(QUERY, limit=7, fusion="interleave", project=HERE)) == base


CREATED = 1735689600.0                  # 2025-01-01T00:00:00Z, invented
NOW = CREATED + 40 * 86400.0
# (id, score) in order, measured from the scoring before the project work on this same invented database:
# a shown-but-never-opened note, a note waiting in the review queue, and recency decay on every note.
OLD_RANKING = [(1, 0.08595), (6, 0.07201), (7, 0.06157), (4, 0.05898), (2, 0.05542), (5, 0.05206), (3, 0.03031)]


def test_the_ranking_without_a_project_is_the_stored_expectation():
    """No project must leave every score where it was: the pairs and their order are a measurement, not another run."""
    s, _, others = make_store(track_access=False)
    with s.conn:
        s.conn.execute("UPDATE notes SET created_at=?, last_access=NULL", (CREATED,))
        s.conn.execute("UPDATE notes SET shown_count=5, opened_count=0 WHERE id=?", (others[0],))
        s.conn.execute("UPDATE notes SET review='pending' WHERE id=?", (others[1],))
    got = [(h["id"], h["score"]) for h in s.search(QUERY, limit=7, now=NOW)]
    assert got == OLD_RANKING
    assert [(h["id"], h["score"]) for h in s.search(QUERY, limit=7, now=NOW, project=None)] == OLD_RANKING


# -- the floor ---------------------------------------------------------------------------------------------------

def test_the_floor_skips_a_penalty_that_would_push_the_combined_multiplier_under_a_quarter():
    rule = ProjectRule.make(HERE, 0.5, parse_kinds("chat"), ProjectMatcher(), 0.5)
    args = (OTHER, "chat", 0, 0, 1, 0.5)
    assert PROJECT_FLOOR == 0.25
    assert rule.factor(*args, combined=1.0) == 0.5
    assert rule.factor(*args, combined=0.5) == 0.5          # 0.25 exactly is not below the floor
    assert rule.factor(*args, combined=0.49) == 1.0         # 0.245 is
    assert rule.factor(*args, combined=0.1) == 1.0
    assert rule.raw(*args) == 0.5                           # the floor is not part of raw()


def test_in_a_search_the_combined_multiplier_is_the_product_of_the_other_adjustments():
    s, first, others = make_store(filler_project=None)
    with s.conn:
        s.conn.execute("UPDATE notes SET review='pending' WHERE id IN (?, ?)", (first, others[0]))
    other_pending = others[0]
    # pending weight 0.4: first's combined multiplier is about 0.4 * (recency 0.85..1) and 0.4 * 0.5 < 0.25: floor applies
    weights = {"pending": 0.4}
    plain = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, weights=weights)}
    withp = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, weights=weights, project=HERE)}
    assert withp[first] == plain[first]                     # skipped by the floor
    # the default pending weight 0.6: 0.6 * ~0.99 * 0.5 is above the floor, so the same note is demoted
    plain = {h["id"]: h["score"] for h in s.search(QUERY, limit=7)}
    withp = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, project=HERE)}
    assert withp[first] == pytest.approx(plain[first] * 0.5, rel=1e-3)
    assert withp[other_pending] == plain[other_pending]     # same project (none): untouched


def test_a_stale_state_note_of_another_project_stays_above_the_floor():
    s = DecisionStore(":memory:")
    for t in FILLER:
        s.add(t, kind="chat", project=HERE)
    first, _ = s.add("orbit scheduler retry policy", keywords="orbit scheduler", kind="chat", project=OTHER)
    s.stale_ids = lambda ids: {i: 99 for i in ids if i == first}        # stale (0.55) and pending (0.6) together: 0.33 * 0.5 < 0.25 is false only above the floor
    with s.conn:
        s.conn.execute("UPDATE notes SET review='pending' WHERE id=?", (first,))
    plain = {h["id"]: h["score"] for h in s.search(QUERY, limit=7)}
    withp = {h["id"]: h["score"] for h in s.search(QUERY, limit=7, project=HERE)}
    assert withp[first] == plain[first]                     # 0.6 * 0.55 * ~0.99 = 0.33; * 0.5 = 0.16 < 0.25: skipped


# -- the vector path ---------------------------------------------------------------------------------------------

class FakeVec:
    """The parts of vec.VectorIndex the provider calls: a nearest-note list with the given ids first."""
    def __init__(self, first_ids):
        self.first_ids = list(first_ids)
        self.calls = []

    def available(self):
        return True

    def index_missing(self, store, n):
        return 0

    def search(self, query, timeout=None):
        self.calls.append(query)
        return [(i, 1.0 - 0.01 * k) for k, i in enumerate(self.first_ids)]

    def close(self):
        pass


def provider_with(pkg, tmp_path, cwd=HERE, **cfg):
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db", "consolidate_on_init": False, **cfg})
    kw = {} if cwd is None else {"cwd": cwd}
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram", **kw)
    return p


def fill(p, kind="chat", first_project=OTHER, filler_project=HERE, first_text="orbit scheduler retry policy",
         first_keywords="orbit scheduler"):
    first, _ = p._store.add(first_text, keywords=first_keywords, kind=kind, project=first_project)
    others = [p._store.add(t, kind="chat", project=filler_project)[0] for t in FILLER]
    return first, others


def fused_ids(p, notes, n=4, project="auto"):
    project = p._project if project == "auto" else project
    return ids(p._vector_fuse([QUERY], notes, n, {}, index=False, project=project))


def test_a_vector_hit_cannot_bring_a_lexical_hit_of_another_project_back_to_the_top(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        lexical = p._store.search(QUERY, limit=7)                     # no project: the first note leads
        assert ids(lexical)[0] == first
        p._vec = FakeVec([first] + others[:3])                        # and the index puts it at rank 1 too
        assert fused_ids(p, lexical, project=None)[0] == first        # without the rule it stays on top
        assert fused_ids(p, lexical)[0] != first                      # with the rule it is demoted ...
        assert first in fused_ids(p, lexical, n=7)                    # ... and still there
    finally:
        p.shutdown()


def test_an_unlisted_kind_in_the_same_position_stays_on_top_of_the_fusion(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p, kind="tech")
        p._vec = FakeVec([first] + others[:3])
        assert fused_ids(p, p._store.search(QUERY, limit=7))[0] == first
    finally:
        p.shutdown()


def test_a_vector_only_hit_of_another_project_is_demoted_too(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p, first_text="completely unrelated gardening remark", first_keywords="")
        lexical = p._store.search(QUERY, limit=7)
        assert first not in ids(lexical)                              # only the index finds it
        p._vec = FakeVec([first])
        base = fused_ids(p, lexical, project=None)
        assert first in base[:2]                                      # rank 1 of the index puts it near the top
        got = fused_ids(p, lexical)
        assert first not in got[:4] and got.index(first) > base.index(first)
        assert first in fused_ids(p, lexical, n=20)                   # demoted, not dropped
    finally:
        p.shutdown()


def test_the_vector_rule_spares_pinned_used_and_same_project_notes(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p, first_text="completely unrelated gardening remark", first_keywords="")
        p._store.conn.execute("UPDATE notes SET pinned=1 WHERE id=?", (first,))
        p._store.conn.commit()
        p._vec = FakeVec([first] + others[:3])
        lexical = p._store.search(QUERY, limit=7)
        assert fused_ids(p, lexical)[:2] == fused_ids(p, lexical, project=None)[:2]
        assert p._store.project_factors([first], HERE) == {}
    finally:
        p.shutdown()


def test_the_prefetch_demotes_other_projects_notes_with_and_without_vectors(pkg, tmp_path):
    p = provider_with(pkg, tmp_path, prefetch_vectors=True)
    try:
        first, others = fill(p, first_text="completely unrelated gardening remark", first_keywords="")
        p._vec = FakeVec([first])
        assert f"#{first} " not in p.prefetch(QUERY)                  # vector-only hit of another project: out of the top 4
        p._project = None
        assert f"#{first} " in p.prefetch(QUERY)                      # the same recall without a project lists it
    finally:
        p.shutdown()
    p = provider_with(pkg, tmp_path / "b")
    try:
        first, _ = fill(p)
        assert f"#{first} " not in p.prefetch(QUERY)
        p._project = None
        assert f"#{first} " in p.prefetch(QUERY)
    finally:
        p.shutdown()


def stale_first(p, first):
    p._store.stale_ids = lambda ids: {i: 99 for i in ids if i == first}


def test_a_floored_pending_and_stale_note_stays_unpenalised_in_the_fused_ordering(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        with p._store.conn:
            p._store.conn.execute("UPDATE notes SET review='pending' WHERE id=?", (first,))
        stale_first(p, first)
        p._vec = FakeVec([first] + others[:3])
        lexical = p._store.search(QUERY, limit=7, project=p._project)
        base = fused_ids(p, lexical, project=None)
        got = fused_ids(p, lexical)
        assert got == base                                       # 0.6 * 0.55 * ~0.99 * 0.5 is under the floor
        assert p._store.project_factors([first], HERE, {first: 0.6 * 0.55}) == {}
    finally:
        p.shutdown()


def test_a_floored_vector_only_note_stays_unpenalised_in_the_fused_ordering(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, _ = fill(p, first_text="completely unrelated gardening remark", first_keywords="")
        with p._store.conn:
            p._store.conn.execute("UPDATE notes SET review='pending' WHERE id=?", (first,))
        stale_first(p, first)
        p._vec = FakeVec([first])
        lexical = p._store.search(QUERY, limit=7, project=p._project)
        assert first not in ids(lexical)
        seen = {}
        real = p._store.project_factors

        def spy(ids, project, combined=None):
            seen["combined"], seen["factors"] = combined, real(ids, project, combined)
            return seen["factors"]

        p._store.project_factors = spy
        base = fused_ids(p, lexical, project=None)
        assert fused_ids(p, lexical) == base
        assert seen["combined"][first] * 0.5 < PROJECT_FLOOR   # pending * stale * recency, not the review weight alone
        assert first not in seen["factors"]                    # so the fused ordering did not multiply it at all
    finally:
        p.shutdown()


def test_a_stale_note_above_the_floor_is_still_demoted_by_the_fused_ordering(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, _ = fill(p, first_text="completely unrelated gardening remark", first_keywords="")
        stale_first(p, first)                                    # stale only: 0.55 * ~0.99 * 0.5 is above the floor
        p._vec = FakeVec([first])
        lexical = p._store.search(QUERY, limit=7, project=p._project)
        base = fused_ids(p, lexical, project=None)
        got = fused_ids(p, lexical)
        assert got != base and got.index(first) > base.index(first) and first in got
    finally:
        p.shutdown()


def test_the_fused_ordering_is_unchanged_when_the_rule_cannot_apply(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        p._vec = FakeVec([first] + others[:3])
        lexical = p._store.search(QUERY, limit=7)
        without_argument = ids(p._vector_fuse([QUERY], lexical, 4, {}, index=False))
        assert fused_ids(p, lexical, project=None) == without_argument
        assert fused_ids(p, lexical, project="") == without_argument
    finally:
        p.shutdown()
    for name, cfg in (("penalty", {"project_penalty": 1.0}), ("kinds", {"project_penalty_kinds": ""})):
        p = provider_with(pkg, tmp_path / name, **cfg)
        try:
            first, others = fill(p)
            p._vec = FakeVec([first] + others[:3])
            lexical = p._store.search(QUERY, limit=7, project=p._project)
            assert fused_ids(p, lexical) == ids(p._vector_fuse([QUERY], lexical, 4, {}, index=False))
        finally:
            p.shutdown()


def test_the_prefetch_gives_the_fusion_the_multiplier_the_rescoring_computed(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        p._vec = FakeVec([first] + others[:3])
        seen = {}
        real = p._vector_fuse

        def spy(*a, **k):
            seen.update(k)
            return real(*a, **k)

        p._vector_fuse = spy
        p.prefetch(QUERY)
        assert seen["project"] == p._project
        assert 0.0 < seen["combined"][first] < 1.0              # trust * idle * pending * open rate * stale * recency
    finally:
        p.shutdown()


# -- only the automatic recall passes a project ---------------------------------------------------------------

def test_the_prefetch_passes_the_project_and_nothing_else_does(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        p._vec = FakeVec([first] + others[:3])
        calls = []
        for name in ("search", "search_multi", "reflect", "related", "list", "get", "history", "notes_by_ids"):
            real = getattr(p._store, name)
            setattr(p._store, name, (lambda real, name: lambda *a, **k: (calls.append((name, k.get("project"))), real(*a, **k))[1])(real, name))
        fuse = p._vector_fuse
        p._vector_fuse = lambda *a, **k: (calls.append(("fuse", k.get("project"))), fuse(*a, **k))[1]
        p.handle_tool_call("memory_search", {"query": QUERY})
        p.handle_tool_call("memory_search", {"query": QUERY, "queries": ["scheduler orbit"]})
        p.handle_tool_call("memory_reflect", {"query": QUERY})
        p.handle_tool_call("memory_related", {"entity": "orbit"})
        p.handle_tool_call("memory_list", {})
        p.handle_tool_call("memory_get", {"id": first})
        p.handle_tool_call("memory_remember", {"content": "orbit scheduler runs in utc", "keywords": "a b c d e"})
        assert calls and all(proj is None for _, proj in calls), calls
        calls.clear()
        p.prefetch(QUERY)
        assert ("search", p._project) in calls and ("fuse", p._project) in calls
        assert all(proj in (None, p._project) for _, proj in calls)
    finally:
        p.shutdown()


def test_the_explicit_search_ranks_the_other_projects_note_first_as_before(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        first, others = fill(p)
        out = json.loads(p.handle_tool_call("memory_search", {"query": QUERY}))
        assert out["results"][0]["id"] == first
        assert "project" not in json.dumps(out) and OTHER not in json.dumps(out)
        p._vec = FakeVec([first] + others[:3])
        out = json.loads(p.handle_tool_call("memory_search", {"query": QUERY}))
        assert out["results"][0]["id"] == first
    finally:
        p.shutdown()


def test_the_recall_text_shows_no_project(pkg, tmp_path):
    p = provider_with(pkg, tmp_path)
    try:
        fill(p)
        text = p.prefetch(QUERY)
        assert text and OTHER not in text and HERE not in text and "project" not in text.lower()
    finally:
        p.shutdown()


def test_a_session_without_a_project_recalls_exactly_as_before(pkg, tmp_path):
    a = provider_with(pkg, tmp_path / "a", cwd=None)
    b = provider_with(pkg, tmp_path / "b", cwd=HERE, project_penalty=1.0)
    c = provider_with(pkg, tmp_path / "c", cwd=HERE, project_penalty_kinds="")
    try:
        texts = []
        for p in (a, b, c):
            fill(p)
            texts.append(p.prefetch(QUERY))
        assert texts[0] == texts[1] == texts[2] and texts[0]
    finally:
        for p in (a, b, c):
            p.shutdown()


def test_the_configuration_reaches_the_store(pkg, tmp_path):
    p = provider_with(pkg, tmp_path, project_penalty=0.3, project_penalty_kinds="tech")
    try:
        assert p._store.project_penalty == 0.3 and p._store.project_penalty_kinds == frozenset({"tech"})
    finally:
        p.shutdown()
    p = provider_with(pkg, tmp_path / "d")
    try:
        assert p._store.project_penalty == 0.5 and p._store.project_penalty_kinds == frozenset({"chat", "summary"})
    finally:
        p.shutdown()


def test_parsing_of_the_two_settings():
    assert parse_penalty("0.3") == 0.3 and parse_penalty(2) == 1.0 and parse_penalty(-1) == 0.0
    assert parse_penalty("nonsense") == 0.5 and parse_penalty(None) == 0.5 and parse_penalty(float("nan")) == 0.5
    assert parse_kinds(None) == frozenset({"chat", "summary"})
    assert parse_kinds("") == frozenset() and parse_kinds([]) == frozenset()
    assert parse_kinds("Chat, tech  STATE") == frozenset({"chat", "tech", "state"})
