"""tools/sleep_consolidate.py (LLM-assisted nightly review; dry run by default)."""
import importlib.util
import io
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()


@pytest.fixture(scope="module")
def sc(pkg):
    spec = importlib.util.spec_from_file_location("sleep_consolidate_tool", ROOT / "tools" / "sleep_consolidate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def review_batch(self, text):
        self.calls.append(text)
        return self.answer(text) if callable(self.answer) else self.answer


def _db(tmp_path, store_mod, build):
    path = tmp_path / "t.db"
    s = store_mod.DecisionStore(str(path))
    ids = build(s)
    s.close()
    return path, ids


def _run(sc, path, llm, *flags):
    args = sc.build_parser().parse_args(["--db", str(path), *flags])
    out = io.StringIO()
    code = sc.run(args, llm=llm, out=out, now=NOW)
    return code, out.getvalue()


def _fixture_notes(s):
    old = s.add("Zorblax 備份每日 03:00 執行，保留 7 日", kind="tech", created_at=NOW - 20 * 86400)[0]
    dup = s.add("Zorblax 備份每日 03:00 執行並保留 7 日，上傳到 R2", kind="tech", created_at=NOW - 3600)[0]
    state_old = s.add("Quuxlet 目前版本為 1.2", kind="state", created_at=NOW - 10 * 86400, entities=["Quuxlet"])[0]
    state_new = s.add("Quuxlet 已升級到版本 1.4", kind="state", created_at=NOW - 7200, entities=["Quuxlet"])[0]
    pinned = s.add("釘選：永遠先備份再部署", kind="decision", pinned=True, created_at=NOW - 5400)[0]
    return dict(old=old, dup=dup, state_old=state_old, state_new=state_new, pinned=pinned)


def _snapshot(path, store_mod):
    s = store_mod.DecisionStore(str(path))
    rows = [tuple(r) for r in s.conn.execute("SELECT id, status, canonical_id, valid_to, valid_from, review, content FROM notes ORDER BY id")]
    s.close()
    return rows


def test_no_llm_configured_prints_message_exit_zero(sc, tmp_path, store_mod, monkeypatch):
    for v in ("HERMIND_LLM_BASE_URL", "HERMIND_LLM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    path, _ = _db(tmp_path, store_mod, _fixture_notes)
    before = _snapshot(path, store_mod)
    code, out = _run(sc, path, None)
    assert code == 0 and "No LLM configured" in out
    assert _snapshot(path, store_mod) == before


def test_missing_db_is_an_error(sc, tmp_path):
    code = sc.run(sc.build_parser().parse_args(["--db", str(tmp_path / "nope.db")]), llm=FakeLLM({}), out=io.StringIO())
    assert code == 2


def test_dry_run_is_default_and_changes_nothing(sc, tmp_path, store_mod):
    path, ids = _db(tmp_path, store_mod, _fixture_notes)
    before = _snapshot(path, store_mod)
    llm = FakeLLM({"merges": [{"keep": ids["dup"], "drop": [ids["old"]], "reason": "same backup schedule"}],
                   "supersessions": [{"old": ids["state_old"], "new": ids["state_new"], "reason": "upgraded"}],
                   "aggregates": [{"text": "Zorblax 與 Quuxlet 都由用戶自己維護", "kind": "state",
                                   "sources": [ids["dup"], ids["state_new"]], "reason": "pattern"}]})
    code, out = _run(sc, path, llm)
    assert code == 0 and "DRY RUN" in out and "nothing was changed" in out
    assert "MERGES: 1 accepted" in out and "SUPERSESSIONS" in out and "AGGREGATE FACTS" in out
    assert "would be applied with --apply" in out
    assert _snapshot(path, store_mod) == before
    assert llm.calls and "RECENT notes" in llm.calls[0]


def test_apply_merges_supersedes_and_adds_pending_aggregate_then_restores(sc, tmp_path, store_mod):
    path, ids = _db(tmp_path, store_mod, _fixture_notes)
    llm = FakeLLM({"merges": [{"keep": ids["dup"], "drop": [ids["old"]], "reason": "same"}],
                   "supersessions": [{"old": ids["state_old"], "new": ids["state_new"], "reason": "upgraded"}],
                   "aggregates": [{"text": "Zorblax 與 Quuxlet 都由用戶自己維護，備份與版本都要追蹤", "kind": "state",
                                   "sources": [ids["dup"], ids["state_new"]], "reason": "pattern"}]})
    code, out = _run(sc, path, llm, "--apply")
    assert code == 0 and "APPLIED" in out and "--restore" in out
    s = store_mod.DecisionStore(str(path))
    try:
        old, state_old, state_new = s.get(ids["old"]), s.get(ids["state_old"]), s.get(ids["state_new"])
        assert old["status"] == "merged" and old["canonical_id"] == ids["dup"]            # hidden, not deleted
        assert state_old["status"] == "superseded" and state_old["valid_to"] is not None
        assert state_new["valid_from"] is not None
        new = [r for r in s.conn.execute("SELECT * FROM notes WHERE source_session LIKE 'sleep:%'")]
        assert len(new) == 1 and new[0]["review"] == "pending" and new[0]["status"] == "active"
        assert f"#{ids['dup']}" in new[0]["content"] and f"#{ids['state_new']}" in new[0]["content"]
        assert s.count() == 6                                                              # 5 + the aggregate; none deleted
        assert [h["id"] for h in s.search("Quuxlet 版本")][0] == ids["state_new"]
        assert ids["old"] not in [h["id"] for h in s.search("Zorblax 備份", limit=10)]
    finally:
        s.close()
    # undo
    code, out = _run(sc, path, None, "--restore", str(ids["old"]), str(ids["state_old"]))
    assert "restored" in out
    s = store_mod.DecisionStore(str(path))
    try:
        assert s.get(ids["old"])["status"] == "active" and s.get(ids["state_old"])["status"] == "active"
        assert s.get(ids["state_old"])["valid_to"] is None
    finally:
        s.close()
    # applying again does not stack a second aggregate
    code, out = _run(sc, path, llm, "--apply")
    s = store_mod.DecisionStore(str(path))
    assert s.conn.execute("SELECT COUNT(*) FROM notes WHERE source_session LIKE 'sleep:%'").fetchone()[0] == 1
    s.close()


def test_guards_reject_bad_proposals(sc, tmp_path, store_mod):
    path, ids = _db(tmp_path, store_mod, _fixture_notes)
    llm = FakeLLM({"merges": [
        {"keep": ids["dup"], "drop": [999999], "reason": "ghost id"},
        {"keep": ids["dup"], "drop": [ids["pinned"]], "reason": "pinned"},
        {"keep": ids["dup"], "drop": [ids["dup"]], "reason": "self"}],
        "supersessions": [{"old": ids["state_new"], "new": ids["state_old"], "reason": "backwards"},
                          {"old": ids["pinned"], "new": ids["state_new"], "reason": "pinned"}],
        "aggregates": [
            {"text": "用戶總共有 99 個伺服器", "kind": "state", "sources": [ids["dup"], ids["state_new"]]},
            {"text": "只有一個來源的結論 more than twelve chars", "kind": "state", "sources": [ids["dup"]]},
            {"text": "引用不存在的來源也不行 invented", "kind": "state", "sources": [ids["dup"], 424242]},
            {"text": "token is ghp_" + "a1B2c3D4" * 5 + " shared", "kind": "tech", "sources": [ids["dup"], ids["old"]]}]})
    before = _snapshot(path, store_mod)
    code, out = _run(sc, path, llm, "--apply")
    assert code == 0
    assert "MERGES: 0 accepted, 3 rejected" in out
    assert "SUPERSESSIONS (older note gets valid_to): 0 accepted, 2 rejected" in out
    assert "AGGREGATE FACTS (new pending notes): 0 accepted, 4 rejected" in out
    for why in ("unknown note id", "never hidden", "not newer", "no source has", "at least two real source"):
        assert why in out, why
    assert _snapshot(path, store_mod) == before                                # nothing applied


def test_merge_may_not_lose_a_number(sc, tmp_path, store_mod):
    def build(s):
        a = s.add("Zorblax 備份每日 03:00 執行", kind="tech", created_at=NOW - 7200)[0]
        b = s.add("Zorblax 備份每日 03:00 執行，保留 7 日", kind="tech", created_at=NOW - 3600)[0]
        return a, b
    path, (a, b) = _db(tmp_path, store_mod, build)
    llm = FakeLLM({"merges": [{"keep": a, "drop": [b], "reason": "dup"}]})
    code, out = _run(sc, path, llm)
    assert "lacks a number" in out


def test_pending_note_cannot_hide_a_confirmed_one(sc, tmp_path, store_mod):
    def build(s):
        a = s.add("Zorblax 備份每日 03:00 執行", kind="tech", created_at=NOW - 7200)[0]
        b = s.add("Zorblax 備份每日 03:00 執行並上傳", kind="tech", created_at=NOW - 3600, review="pending")[0]
        return a, b
    path, (a, b) = _db(tmp_path, store_mod, build)
    code, out = _run(sc, path, FakeLLM({"merges": [{"keep": b, "drop": [a], "reason": "dup"}]}))
    assert "unconfirmed" in out


def test_llm_failure_is_fail_soft(sc, tmp_path, store_mod):
    path, _ = _db(tmp_path, store_mod, _fixture_notes)
    before = _snapshot(path, store_mod)
    code, out = _run(sc, path, FakeLLM(None), "--apply")
    assert code == 0 and "no usable answer" in out and _snapshot(path, store_mod) == before


def test_window_and_limit(sc, tmp_path, store_mod):
    path, ids = _db(tmp_path, store_mod, _fixture_notes)
    llm = FakeLLM({})
    _run(sc, path, llm, "--since-hours", "3", "--limit", "2")
    assert llm.calls
    recent_part = llm.calls[0].split("OLDER notes")[0]
    assert len([ln for ln in recent_part.splitlines() if " | " in ln]) == 2      # at most 2 recent notes
    assert "Quuxlet 目前版本為 1.2" not in recent_part       # created 10 days ago: outside the window
    code, out = _run(sc, path, FakeLLM({}), "--since-hours", "0.0001")
    assert "Nothing to do" in out


def test_max_apply_limits_changes(sc, tmp_path, store_mod):
    path, ids = _db(tmp_path, store_mod, _fixture_notes)
    llm = FakeLLM({"merges": [{"keep": ids["dup"], "drop": [ids["old"]], "reason": "same"}],
                   "supersessions": [{"old": ids["state_old"], "new": ids["state_new"], "reason": "upgraded"}]})
    code, out = _run(sc, path, llm, "--apply", "--max-apply", "1")
    assert "left for the next run" in out


def test_llm_review_batch_parses_and_fails_soft(pkg):
    from hermind.llm import LLMClient
    c = LLMClient({"base_url": "http://x", "model": "m"})
    c.chat = lambda *a, **k: 'noise {"merges": [{"keep": 1, "drop": [2]}], "supersessions": "bad", "aggregates": []} tail'
    assert c.review_batch("x") == {"merges": [{"keep": 1, "drop": [2]}], "supersessions": [], "aggregates": []}
    c.chat = lambda *a, **k: None
    assert c.review_batch("x") is None
    c.chat = lambda *a, **k: "not json"
    assert c.review_batch("x") is None


def test_build_llm_from_flags_env_and_file(sc, tmp_path, monkeypatch):
    p = sc.build_parser()
    assert sc.build_llm(p.parse_args([]), env={}) is None
    c = sc.build_llm(p.parse_args(["--llm-base-url", "http://h/v1", "--llm-model", "m"]), env={})
    assert c.base_url == "http://h/v1" and c.model == "m"
    c = sc.build_llm(p.parse_args([]), env={"HERMIND_LLM_BASE_URL": "http://e/v1", "HERMIND_LLM_MODEL": "em"})
    assert c.model == "em"
    f = tmp_path / "llm.json"
    f.write_text(json.dumps({"base_url": "http://f/v1", "model": "fm", "extra_body": {"thinking": {"type": "disabled"}}}))
    c = sc.build_llm(p.parse_args(["--llm-config", str(f)]), env={})
    assert c.model == "fm" and c.extra_body == {"thinking": {"type": "disabled"}}
