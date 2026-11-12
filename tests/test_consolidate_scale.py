"""consolidate(): batched commits, optional time budget, resumable, same result as an unbudgeted run."""

import threading
import time


def _fill(store, n=60, dups=10):
    for i in range(n):
        store.add(f"project alpha{i} uses gadget{i} with widget{i} behind proxy{i} during sprint{i}", kind="tech")
    for i in range(dups):  # exact-ish duplicates of the first notes
        store.add(f"project alpha{i} uses gadget{i} with widget{i} behind proxy{i} during sprint{i} today", kind="tech")


def _status(store):
    return dict(store.conn.execute("SELECT status, COUNT(*) FROM notes GROUP BY 1").fetchall())


def test_zero_budget_runs_one_batch_then_next_call_resumes(store):
    _fill(store)
    first = store.consolidate(time_budget=0, batch=5)
    assert first["examined"] == 5 and first["remaining"] == 65
    rest = store.consolidate(batch=5)
    assert rest["remaining"] == 0 and rest["merged"] >= 1


def test_budgeted_runs_end_with_same_result_as_one_full_run(store, tmp_path):
    _fill(store)
    r = {}
    for _ in range(100):   # budget that allows roughly one batch per call
        r = store.consolidate(time_budget=1e-9, batch=4)
        if r["remaining"] == 0:
            break
    assert r["remaining"] == 0
    other = type(store)(str(tmp_path / "other.db"))
    _fill(other)
    other.consolidate()
    assert _status(store) == _status(other)


def test_lock_is_released_between_batches(store):
    _fill(store, n=80, dups=0)
    got = []

    def writer():
        t = time.monotonic()
        store.add("a late note written while consolidation runs", kind="chat")
        got.append(time.monotonic() - t)

    orig = store._similar_candidates
    started = []

    def slow(nid, dfs, limit_terms=8):
        if not started:
            started.append(1)
            threading.Thread(target=writer).start()
        time.sleep(0.002)
        return orig(nid, dfs, limit_terms)

    store._similar_candidates = slow
    store.consolidate(batch=5)
    time.sleep(0.2)
    assert got, "writer never finished"
    # the writer got in between batches instead of waiting for all 80 notes (80 * 2 ms = 160 ms)
    assert got[0] < 0.15


def test_remaining_is_reported_and_zero_when_done(store):
    _fill(store, n=10, dups=0)
    assert store.consolidate()["remaining"] == 0


def test_maintenance_has_a_default_time_budget_and_none_means_no_limit(store, monkeypatch):
    """maintenance() runs at session start: by default it may not consolidate for longer than MAINTENANCE_BUDGET."""
    import store_consolidate
    seen = []
    monkeypatch.setattr(type(store), "consolidate", lambda self, **kw: seen.append(kw.get("time_budget")) or {})
    store.maintenance()
    store.maintenance(time_budget=None)
    assert seen == [store_consolidate.MAINTENANCE_BUDGET, None] and 0 < store_consolidate.MAINTENANCE_BUDGET <= 30
