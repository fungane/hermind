"""v0.2 features: normalisation, dates, kinds, entity graph, consolidation, RRF retrieval, reflect, migration."""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

DAY = 86400


def test_normalize_simplified_fullwidth(store_mod):
    tn = sys.modules["hermind.textnorm"]
    assert tn.normalize("用户应交给") == tn.normalize("用戶應交給")
    assert tn.normalize("ＶＰＳ，") == "vps,"
    assert set(tn.tokenize("寫剧本")) & set(tn.tokenize("寫劇本")) >= {"寫劇", "劇本"}


def test_stem_and_compound_parts(store_mod):
    toks = store_mod.tokenize("closing llama.cpp-server")
    assert "clos" in toks and "llama.cpp-server" in toks and "llama" in toks and "server" in toks
    assert store_mod.tokenize("closed")[0] == store_mod.tokenize("close")[0]


def test_parse_dates_and_query_window():
    tn = sys.modules["hermind.textnorm"]
    ref = time.mktime((2026, 10, 19, 12, 0, 0, 0, 0, -1))
    d = tn.parse_dates("When: 2026-10-18 | 另見 10月14日 和 October 15, 2026", ref=ref)
    days = [time.localtime(x)[:3] for x in d]
    assert days == [(2026, 10, 18), (2026, 10, 14), (2026, 10, 15)]
    w = tn.query_time_window("琴日決定咗咩", ref)
    assert w and time.localtime(w[0])[:3] == (2026, 10, 18)


def test_kind_classifier_distribution():
    tn = sys.modules["hermind.textnorm"]
    assert tn.classify_kind("最後決定用方案 B") == "decision"
    assert tn.classify_kind("User prefers short answers with bullet points") == "preference"
    assert tn.classify_kind("Build server currently has 640MB available RAM | When: 2026-03-02") == "state"
    assert tn.classify_kind("Run `hermes gateway restart` after editing ~/.hermes/config.yaml") == "tech"
    assert tn.classify_kind("Google 宣布推出新模型") == "news"
    assert tn.classify_kind("Telegram Hermes Mac VPS") == "chat"


def test_entity_extraction_patterns(store_mod):
    ents = store_mod.extract_entities("The Hermes Agent on VPS 203.0.113.7 uses llama.cpp-server, port 8080 and 「週報」")
    keys = set(ents)
    assert {"hermes agent", "vps", "203.0.113.7", "llama.cpp-server", "8080", "週報"} <= keys
    assert "the" not in keys and "the vps" not in keys


def test_entity_graph_tables_and_alias_detection(store):
    a, _ = store.add("Hindsight 用 Nous 嘅 longcat 模型做抽取", kind="tech", entities=["Hindsight=回憶庫|記憶服務"])
    store.add("hindsight 每次抽取要 3 秒", kind="tech")  # lowercase: linked through the known-entity dictionary
    eid = store.conn.execute("SELECT id FROM entities WHERE key='hindsight'").fetchone()[0]
    linked = {r[0] for r in store.conn.execute("SELECT note_id FROM note_entities WHERE entity_id=?", (eid,))}
    assert len(linked) == 2
    assert eid in store.detect_entities("個回憶庫點設定")  # Cantonese alias
    assert store.conn.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0] >= 1


def test_synonym_groups_seed_entity_aliases(store):
    store.add("VPS 每月 5 美元", kind="decision")
    assert store.detect_entities("部伺服器幾錢")  # VPS <- 伺服器 via synonyms.yaml group


def test_one_hop_graph_expansion(store):
    t = time.time()
    store.add("Zephyrbox 部機用 Ubuntu 24.04", kind="tech", created_at=t)
    target, _ = store.add("Zephyrbox 嘅 swap 設為 2GB", kind="tech", created_at=t)
    for i in range(5):
        store.add(f"無關筆記 {i}：今日食咗飯", kind="chat", created_at=t)
    res = store.search("Ubuntu 版本", limit=5, explain=True)
    ids = [r["id"] for r in res]
    assert target in ids  # shares the entity Zephyrbox with the lexical hit
    assert "graph" in next(r for r in res if r["id"] == target)["signals"]


def test_consolidate_merges_near_duplicates_and_restores(store):
    t = time.time()
    a, _ = store.add("測試機的系統選用 Debian 12，因為相容性最好", kind="decision", created_at=t - 50)
    b, _ = store.add("測試機的系統選用 Debian 12，因為相容性最好、文件最齊全", kind="decision", created_at=t)
    store.add("週報逢星期一 09:00 發送", kind="decision")
    st = store.consolidate()
    assert st["merged"] == 1
    assert store.get(a)["status"] == "merged" and store.get(a)["canonical_id"] == b
    assert store.get(b)["proof_count"] == 2
    res = store.search("VPS 用咩系統")
    assert [r["id"] for r in res].count(b) == 1 and a not in [r["id"] for r in res]
    assert a in [r["id"] for r in store.search("VPS 用咩系統", include_history=True)]
    assert store.get(a) is not None  # never deleted
    assert store.restore(a) and store.get(a)["status"] == "active" and store.get(b)["proof_count"] == 1
    assert store.consolidate(full=True)["merged"] == 0  # restored notes are locked


def test_newer_state_supersedes_older_and_keeps_history(store):
    t = time.time()
    old, _ = store.add("目前 Zephyrbox 硬碟剩 13GB，記憶體剩 800MB", kind="state", created_at=t - 3 * DAY)
    new, _ = store.add("目前 Zephyrbox 硬碟剩 21GB，記憶體剩 1.2GB", kind="state", created_at=t)
    st = store.consolidate()
    assert st["superseded"] == 1
    assert store.get(old)["status"] == "superseded" and store.get(old)["canonical_id"] == new
    assert [r["id"] for r in store.search("Zephyrbox 硬碟剩幾多")][0] == new
    hist = store.history(new)
    assert hist and hist[0]["id"] == old and hist[0]["rel"] == "superseded_by"


def test_quantities_block_merging(store):
    t = time.time()
    a, _ = store.add("週報排程改為每日 10:30 發送給用戶，使用 Sonnet 模型", kind="decision", created_at=t - 9)
    b, _ = store.add("週報排程改為每日 10:30 發送給用戶，使用 Sonnet 模型，字數上限 4096", kind="decision", created_at=t)
    c, _ = store.add("週報排程改為每日 10:30 發送給用戶，使用 Sonnet 模型，字數上限 2000", kind="decision", created_at=t)
    store.consolidate()
    assert store.get(b)["status"] == "active" and store.get(c)["status"] == "active"


def test_since_until_filters(store):
    t = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))
    a, _ = store.add("決定把 gateway 搬去 VPS", kind="decision", created_at=t - 5 * DAY)
    b, _ = store.add("決定把 gateway 日誌放外接碟", kind="decision", created_at=t)
    ids = [r["id"] for r in store.search("gateway 決定", since="2026-10-17", now=t)]
    assert ids == [b]
    ids = [r["id"] for r in store.search("gateway 決定", until="2026-10-14", now=t)]
    assert ids == [a]
    with pytest.raises(ValueError):
        store.search("gateway", since="not a date")


def test_temporal_signal_from_query_date(store):
    t = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))
    a, _ = store.add("gateway 重啟過一次 | When: 2026-10-14", kind="tech", created_at=t)
    b, _ = store.add("gateway 重啟過一次，原因是磁碟滿 | When: 2026-03-04", kind="tech", created_at=t)
    assert store.search("10月14日 gateway 重啟", now=t)[0]["id"] == a


def test_rrf_weights_configurable(store_mod, tmp_path):
    s = store_mod.DecisionStore(str(tmp_path / "w.db"), weights={"entity": 0.0, "k": 5})
    assert s.weights["entity"] == 0.0 and s.weights["bm25"] == store_mod.DEFAULT_WEIGHTS["bm25"]
    s.add("Hermes 用 SQLite", kind="decision")
    assert s.search("SQLite", weights={"graph": 0.0})
    s.close()


def test_diversity_pushes_near_identical_down(store):
    t = time.time()
    a, _ = store.add("週報 10:30 發送 Telegram 頻道", kind="decision", created_at=t)
    b, _ = store.add("週報 10:30 發送 Telegram 頻道。", kind="tech", created_at=t)  # different kind: not merged
    c, _ = store.add("週報 字數 超過 4096 會分段", kind="tech", created_at=t)
    ids = [r["id"] for r in store.search("週報 Telegram 發送", limit=3)]
    assert ids.index(c) < ids.index(b) or ids.index(c) < ids.index(a)


def test_reflect_pack(store):
    t = time.time()
    store.add("目前 Zephyrbox 記憶體剩 800MB", kind="state", created_at=t - 3 * DAY)
    store.add("目前 Zephyrbox 記憶體剩 1.2GB", kind="state", created_at=t)
    store.add("決定 Zephyrbox 唔裝 Docker，因為記憶體太緊", kind="decision", created_at=t)
    store.consolidate()
    pack = store.reflect("Zephyrbox 記憶體")
    assert pack["count"] >= 2
    g = pack["groups"][0]
    assert g["entity"] == "Zephyrbox" and all({"id", "date", "kind", "content", "proof_count"} <= set(n)
                                                for n in g["notes"])
    assert any("history" in n for n in g["notes"])  # the superseded 800MB note is shown as history
    json.dumps(pack, ensure_ascii=False)


def test_related(store):
    store.add("Zephyrbox 用 Ubuntu", kind="tech")
    store.add("Zephyrbox 開咗 swap", kind="tech")
    r = store.related("zephyrbox")
    assert r["found"] and len(r["notes"]) == 2 and any(e["name"] == "Ubuntu" for e in r["related_entities"])
    assert not store.related("nothing-like-this")["found"]


def test_migrates_v01_database(store_mod, tmp_path):
    db = tmp_path / "v01.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat',
      keywords TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL, source_session TEXT NOT NULL DEFAULT '',
      pinned INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '', doc_len_content INTEGER NOT NULL DEFAULT 0,
      doc_len_keywords INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE postings (term TEXT NOT NULL, note_id INTEGER NOT NULL, field INTEGER NOT NULL, tf INTEGER NOT NULL,
      PRIMARY KEY (term, note_id, field)) WITHOUT ROWID;
    """)
    c.execute("INSERT INTO notes(content, kind, keywords, created_at) VALUES (?,?,?,?)",
              ("決定 Hindsight 改用備用模型", "decision", "fallback 後備", time.time()))
    c.commit()
    c.close()
    s = store_mod.DecisionStore(str(db))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    res = s.search("hindsight fallback")
    assert res and res[0]["status"] == "active" and res[0]["proof_count"] == 1
    assert s.related("Hindsight")["found"]
    s.close()


def test_search_uses_sql_candidates_and_is_fast(store):
    t = time.time()
    for i in range(400):
        store.add(f"筆記 {i}：伺服器 {i % 7} 號用 swap {i % 5}GB，項目 P{i}", kind="tech", created_at=t - i)
    t0 = time.perf_counter()
    store.search("伺服器 swap 幾多", limit=10)
    assert time.perf_counter() - t0 < 0.5


def test_provider_reflect_related_and_search_args(provider):
    r = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "決定 Zephyrbox 每日 04:00 自動備份", "kind": "decision",
        "keywords": "backup 備份 定時 排程 自動", "entities": ["Zephyrbox=測試機"]}))
    assert r["created"]
    out = json.loads(provider.handle_tool_call("memory_reflect", {"query": "測試機備份"}))
    assert out["count"] >= 1 and out["groups"][0]["notes"][0]["id"] == r["id"]
    rel = json.loads(provider.handle_tool_call("memory_related", {"entity": "測試機"}))
    assert rel["found"] and rel["notes"][0]["id"] == r["id"]
    res = json.loads(provider.handle_tool_call("memory_search", {"query": "備份", "since": "2000-01-01"}))
    assert res["count"] == 1
    assert "error" in json.loads(provider.handle_tool_call("memory_search", {"query": "備份", "since": "bogus"}))


HERMES_AGENT = Path("/path/to/hermes-agent")


@pytest.mark.skipif(not (HERMES_AGENT / "agent" / "memory_provider.py").exists(), reason="no Hermes checkout")
def test_real_memory_provider_base_class(tmp_path):
    root = Path(__file__).resolve().parent.parent
    code = f"""
import sys, json, importlib.util
sys.path.insert(0, {str(HERMES_AGENT)!r})
from agent.memory_provider import MemoryProvider
spec = importlib.util.spec_from_file_location("hermind", {str(root / '__init__.py')!r},
                                              submodule_search_locations=[{str(root)!r}])
m = importlib.util.module_from_spec(spec); sys.modules["hermind"] = m; spec.loader.exec_module(m)
p = m.HermindProvider(config={{"db_path": {str(tmp_path / 'real.db')!r}}})
assert isinstance(p, MemoryProvider)
p.initialize("20261018_120000_abcd", hermes_home={str(tmp_path)!r}, platform="telegram")
r = json.loads(p.handle_tool_call("memory_remember", {{"content": "決定用 SQLite", "keywords": "資料庫 database db 儲存 sqlite3"}}))
assert json.loads(p.handle_tool_call("memory_reflect", {{"query": "資料庫"}}))["count"] == 1
p.sync_turn("週報幾點？", "10:30")
print("OK", p.prefetch("資料庫用咩") != "")
p.shutdown()
"""
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    if "ModuleNotFoundError" in res.stderr and "agent.memory_provider" not in res.stderr:
        pytest.skip("Hermes dependencies not importable here: " + res.stderr.strip().splitlines()[-1])
    assert res.returncode == 0, res.stderr
    assert "OK True" in res.stdout


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX only")
def test_until_and_since_use_whole_local_days_on_dst_days(store, monkeypatch):
    import os
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        def at(*ymdhm):
            return time.mktime((*ymdhm, 0, 0, 0, -1))
        spring, fall_late, fall_next = (at(2026, 3, 9, 0, 30), at(2026, 11, 1, 23, 30), at(2026, 11, 2, 0, 10))
        ids = {}
        for key, t in (("spring", spring), ("late", fall_late), ("next", fall_next)):
            ids[key], _ = store.add(f"Zorblax backup {key} window runs on node seven", kind="tech", created_at=t)
        now = at(2026, 12, 1, 12, 0)

        def found(**kw):
            via_search = {r["id"] for r in store.search("Zorblax backup", limit=10, now=now, **kw)}
            via_multi = {r["id"] for r in store.search_multi(["Zorblax backup"], limit=10, now=now, **kw)}
            via_ids = {r["id"] for r in store.notes_by_ids(ids.values(), now=now, **kw)}
            assert via_search == via_multi == via_ids, (kw, via_search, via_multi, via_ids)
            return via_search
        # 2026-03-08 is 23 h long after the clocks went forward on the 8th: 00:30 on the 9th is NOT part of it ...
        assert ids["spring"] not in found(until="2026-03-08")
        assert ids["spring"] in found(until="2026-03-09")
        # ... and 2026-11-01 is 25 h long: 23:30 on that day IS part of it, 00:10 on the 2nd is not
        assert ids["late"] in found(until="2026-11-01") and ids["next"] not in found(until="2026-11-01")
        assert found(since="2026-11-02", until="2026-11-02") == {ids["next"]}
        assert found(since="2026-11-01", until="2026-11-01") == {ids["late"]}
        assert found(since="2026-03-09", until="2026-03-09") == {ids["spring"]}
    finally:
        monkeypatch.undo()
        time.tzset()
