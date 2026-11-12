"""open-rate feedback prior (bounded, smoothed, decaying) and its bookkeeping."""
import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

DAY = 86400.0
T0 = 1_790_000_000.0


def _call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


@pytest.fixture
def fb(store_mod):
    return store_mod


# -- the pure formula ---------------------------------------------------------------------------------------------

def test_factor_is_neutral_below_min_shown(fb):
    assert fb.open_rate_factor(4, 4, T0, T0, 0.2) == 1.0
    assert fb.open_rate_factor(0, 0, None, T0, 0.2) == 1.0
    assert fb.open_rate_factor(5, 5, T0, T0, 0.2) > 1.0          # at the threshold it applies


def test_factor_is_always_within_ten_percent(fb):
    for shown, opened, base, age in itertools.product((0, 1, 5, 6, 50, 1000, 1e6), (0, 1, 3, 50, 1e6),
                                                      (0.0, 0.05, 0.2, 0.5, 0.9, 1.0), (0, 30 * DAY, 400 * DAY)):
        f = fb.open_rate_factor(shown, opened, T0, T0 + age, base)
        assert 0.9 <= f <= 1.1, (shown, opened, base, age, f)


def test_factor_moves_with_open_rate_and_is_smoothed(fb):
    base = 0.2
    never = fb.open_rate_factor(20, 0, T0, T0, base)
    meh = fb.open_rate_factor(20, 4, T0, T0, base)              # exactly the corpus rate (4/20 smoothed to 0.2)
    often = fb.open_rate_factor(20, 15, T0, T0, base)
    assert never < meh < often
    assert often == pytest.approx(1.1, abs=1e-9)                  # 15/20 opened: capped
    assert 0.9 <= never < 0.95                                    # 0/20: smoothed (1 pseudo-open), so above the floor
    assert abs(meh - 1.0) < 0.01
    # smoothing: 5/5 opened is much less extreme than the raw rate of 100 %, a mild lead gives a mild effect
    assert fb.open_rate_factor(10, 3, T0, T0, base) < 1.05


def test_factor_decays_to_neutral(fb):
    base = 0.2
    fresh = fb.open_rate_factor(40, 36, T0, T0, base)
    half = fb.open_rate_factor(40, 36, T0, T0 + 90 * DAY, base)
    old = fb.open_rate_factor(40, 36, T0, T0 + 400 * DAY, base)
    assert fresh == pytest.approx(1.1) and half == pytest.approx(1.1)      # 20 effective shows: still capped
    assert old == 1.0                                                      # faded below the minimum
    assert fb.fb_decay(T0, T0 + 90 * DAY) == pytest.approx(0.5)


# -- bookkeeping ----------------------------------------------------------------------------------------------------

def test_record_shown_opened_with_decay(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech")
    assert store.record_shown([nid, nid, 99999], now=T0) == 1
    store.record_opened(nid, now=T0)
    row = store.get(nid)
    assert (row["shown_count"], row["opened_count"], row["fb_at"]) == (1.0, 1.0, T0)
    store.record_shown([nid], now=T0 + 90 * DAY)                  # old counts are halved first
    row = store.get(nid)
    assert row["shown_count"] == pytest.approx(1.5) and row["opened_count"] == pytest.approx(0.5)
    store.record_opened(nid, now=T0 + 90 * DAY)
    row = store.get(nid)
    assert row["opened_count"] <= row["shown_count"]


def test_old_db_gets_columns_default_zero(tmp_path, store_mod):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'chat', keywords TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL,
        source_session TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '',
        doc_len_content INTEGER NOT NULL DEFAULT 0, doc_len_keywords INTEGER NOT NULL DEFAULT 0);
        INSERT INTO notes(content, created_at) VALUES ('legacy note', 1700000000); PRAGMA user_version=4;""")
    c.commit(); c.close()
    s = store_mod.DecisionStore(path)
    r = s.get(1)
    assert (r["shown_count"], r["opened_count"], r["fb_at"]) == (0, 0, None)
    s.close()


# -- provider tracking ------------------------------------------------------------------------------------------------

def store_mod_factor(r, base, now):
    import sys
    f = sys.modules["hermind.store"].open_rate_factor
    return f(r["shown_count"], r["opened_count"], r["fb_at"], now, base)


def _counts(p, nid):
    r = p._store.get(nid)
    return r["shown_count"], r["opened_count"]


def _two(provider):
    a = _call(provider, "memory_remember", content="Quuxlet 預設 port 為 8080，由 Caddy 反向代理", kind="tech",
              keywords="quuxlet port caddy proxy")["id"]
    b = _call(provider, "memory_remember", content="Zorblax 備份每日 03:00 執行並上傳到 R2", kind="tech",
              keywords="zorblax backup r2")["id"]
    return a, b


def test_search_counts_shown_once_per_session_and_get_counts_opened(provider):
    a, b = _two(provider)
    res = _call(provider, "memory_search", query="quuxlet port")
    assert [r["id"] for r in res["results"]] == [a]
    assert _counts(provider, a) == (1, 0) and _counts(provider, b) == (0, 0)
    _call(provider, "memory_get", id=b)                          # never listed in this session -> not an open
    assert _counts(provider, b) == (0, 0)
    _call(provider, "memory_search", query="quuxlet port")       # same session: still one 'shown'
    assert _counts(provider, a) == (1, 0)
    _call(provider, "memory_get", id=a)
    _call(provider, "memory_get", id=a)                          # opened counted once per session
    assert _counts(provider, a) == (1, 1)
    provider.on_session_switch("20261019_090000_zzzz")           # a new session counts again
    _call(provider, "memory_search", query="quuxlet port")
    _call(provider, "memory_get", id=a)
    assert _counts(provider, a) == pytest.approx((2, 2))


def test_prefetch_lines_count_as_shown(provider):
    a, _ = _two(provider)
    assert "#%d" % a in provider.prefetch("quuxlet port 設定")
    assert _counts(provider, a)[0] == 1
    _call(provider, "memory_get", id=a)                          # opening a recalled note is an open too
    assert _counts(provider, a) == (1, 1)


def test_read_only_and_skipped_sessions_do_not_count(provider):
    a, _ = _two(provider)
    provider._writable = False
    _call(provider, "memory_search", query="quuxlet port")
    provider._writable, provider._skip = True, True
    _call(provider, "memory_search", query="quuxlet port")
    assert _counts(provider, a) == (0, 0)


def test_config_switch_disables_tracking_and_prior(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db", "open_rate": False})
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli")
    try:
        a, _ = _two(p)
        _call(p, "memory_search", query="quuxlet port")
        assert _counts(p, a) == (0, 0) and p._store.weights["openrate"] == 0.0
    finally:
        p.shutdown()


# -- ranking: bounded, cannot dominate relevance, no feedback loop ----------------------------------------------------

def _ids(store, q, now=T0, **kw):
    return [h["id"] for h in store.search(q, limit=10, now=now, **kw)]


def _set_fb(store, nid, shown, opened, at=T0):
    store.conn.execute("UPDATE notes SET shown_count=?, opened_count=?, fb_at=? WHERE id=?", (shown, opened, at, nid))
    store.conn.commit()
    store.__dict__.pop("_fb_base", None)


def test_prior_flips_a_near_tie_but_only_by_ten_percent(store):
    a, _ = store.add("Quuxlet 預設 port 為 8080 由 Caddy 代理", kind="tech", created_at=T0 - DAY)
    b, _ = store.add("Quuxlet 預設 port 為 9090 由 Caddy 代理", kind="tech", created_at=T0 - DAY)
    base = store.search("quuxlet port caddy", limit=5, now=T0)
    first, second = base[0]["id"], base[1]["id"]
    _set_fb(store, second, 30, 27)                                 # the runner-up is opened a lot
    _set_fb(store, first, 30, 0)                                   # the leader never is
    after = {h["id"]: h for h in store.search("quuxlet port caddy", limit=5, now=T0)}
    assert after[second]["score"] > after[first]["score"]          # near-tie: the prior decides
    for nid in (first, second):                                    # ...but no score moved by more than 10 %
        ratio = after[nid]["score"] / next(h["score"] for h in base if h["id"] == nid)
        assert 0.9 - 2e-3 <= ratio <= 1.1 + 2e-3


def test_prior_cannot_overturn_a_clear_relevance_lead(store):
    """RRF scores of neighbouring ranks are only a few percent apart, so a +-10 % prior may reorder near-ties (and
    a note a few ranks down); what it must never do is overturn a real lead: a note ahead by more than
    1.1/0.9 = 22 % stays ahead even when it has the worst open rate and the other the best."""
    texts = ["Quuxlet 透過 Caddy proxy 對外開放 port 8443，憑證自動更新", "Caddy proxy 把 Quuxlet 的 port 8080 轉到內部服務",
             "Quuxlet 的 Caddy proxy 設定檔放在 /etc/caddy，port 由環境變數決定",
             "決定 Quuxlet 一律走 Caddy proxy，port 8443 以外全部封鎖", "Quuxlet 上線前先確認 Caddy proxy 與 port 轉發有效",
             "Caddy 的版本是 2.8，裝在 VPS 上", "Quuxlet 是公司內部的小工具"]
    ids = [store.add(t, kind="tech", created_at=T0 - DAY * (i + 1))[0] for i, t in enumerate(texts)]
    q, flat = "quuxlet caddy proxy port", {"diversity": 0}
    base = {h["id"]: h["score"] for h in store.search(q, limit=10, now=T0, weights={**flat, "openrate": 0})}
    ranked = sorted(base, key=lambda i: -base[i])
    for i in ranked[:len(ranked) // 2]:
        _set_fb(store, i, 500, 0)                       # most relevant notes: never opened ...
    for i in ranked[len(ranked) // 2:]:
        _set_fb(store, i, 500, 500)                     # ... least relevant: always opened
    after = {h["id"]: h["score"] for h in store.search(q, limit=10, now=T0, weights=flat)}
    pairs = [(i, j) for i in base for j in base if base[i] > base[j] * (1.1 / 0.9) * 1.001]
    assert pairs, "corpus has no pair with a >22 % lead; the test would be vacuous"
    for i, j in pairs:
        assert after[i] > after[j], (i, j, base[i] / base[j], after[i] / after[j])
    for i in base:
        assert 0.9 - 2e-3 <= after[i] / base[i] <= 1.1 + 2e-3


def test_prior_never_makes_a_non_matching_note_a_result(store):
    store.add("Quuxlet 預設 port 為 8080", kind="tech")
    other, _ = store.add("今日天氣好熱", kind="chat")
    _set_fb(store, other, 1000, 1000)
    assert other not in _ids(store, "quuxlet port")


def test_prior_disabled_by_weight(store):
    a, _ = store.add("Quuxlet 預設 port 為 8080 由 Caddy 代理", kind="tech", created_at=T0 - DAY)
    b, _ = store.add("Quuxlet 預設 port 為 9090 由 Caddy 代理", kind="tech", created_at=T0 - DAY)
    base = {h["id"]: h["score"] for h in store.search("quuxlet port caddy", limit=5, now=T0)}
    _set_fb(store, a, 40, 0); _set_fb(store, b, 40, 40)
    off = {h["id"]: h["score"] for h in store.search("quuxlet port caddy", limit=5, now=T0, weights={"openrate": 0})}
    assert off == base
    on = {h["id"]: h["score"] for h in store.search("quuxlet port caddy", limit=5, now=T0)}
    assert on != base


def test_no_runaway_feedback_loop(store):
    """Worst case: every round the search shows the top 5 and the user opens the top 1 (position bias). The rate
    is relative to the corpus rate and the effect is capped, so scores stay within 10 % of the no-feedback scores
    and the order of clearly separated notes never changes."""
    ids = [store.add(f"Quuxlet Caddy 設定記錄 第 {i} 版 port {8000 + i}" + " 額外說明" * (i % 4), kind="tech",
                     created_at=T0 - DAY * (i + 1))[0] for i in range(8)]
    q = "quuxlet caddy port"
    baseline = {h["id"]: h["score"] for h in store.search(q, limit=10, now=T0)}
    order0 = _ids(store, q)
    now = T0
    for _ in range(300):
        top = _ids(store, q, now=now)
        store.record_shown(top[:5], now=now)
        store.record_opened(top[0], now=now)
        now += 3600
    final = {h["id"]: h["score"] for h in store.search(q, limit=10, now=T0)}
    for nid, s in final.items():
        assert 0.9 - 2e-3 <= s / baseline[nid] <= 1.1 + 2e-3
    base = store.open_rate_base()
    for r in store.conn.execute("SELECT * FROM notes"):
        f = store_mod_factor(r, base, T0)
        assert 0.9 <= f <= 1.1
    assert order0[-1] in _ids(store, q, now=T0)[-3:]               # the bottom of the list did not climb to the top


def test_usage_report_includes_open_rate(store, tmp_path):
    a, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech")
    for _ in range(8):
        store.record_shown([a], now=T0)
    for _ in range(1):
        store.record_opened(a, now=T0)
    rep = store.usage_report(30)["open_rate"]
    assert rep["tracked"] == 1 and rep["shown_total"] >= 1
    assert all(0.9 <= x["factor"] <= 1.1 for x in rep["boosted"] + rep["demoted"])
    store.conn.commit()
    tool = Path(__file__).resolve().parent.parent / "tools" / "usage_report.py"
    out = subprocess.run([sys.executable, "-I", str(tool), store.db_path], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "open-rate prior" in out.stdout
