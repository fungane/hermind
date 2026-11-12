import json
import time

import pytest


def test_tokenize_unigram_bigram_and_latin(store_mod):
    toks = store_mod.tokenize("備選模型 Claude-Code VPS")
    assert {"備", "選", "備選", "選模", "模型"} <= set(toks)
    assert "claude-code" in toks and "vps" in toks


def test_stop_chars_have_no_weight(store_mod):
    assert store_mod.token_weight("嘅") == 0.0
    assert store_mod.token_weight("我們") == 0.0
    assert store_mod.token_weight("下架") > store_mod.token_weight("架") > 0


def test_synonym_file_parses_and_expands_both_ways(store_mod):
    syn = store_mod.Synonyms(store_mod.load_synonyms())
    assert len(syn.groups) >= 80
    assert "下架" in syn.expand("免費模型死咗點算")
    assert "怎麼辦" in syn.expand("點算")
    assert "服務器" in syn.expand("伺服器")  # reverse direction


def test_colloquial_query_finds_note_via_synonyms(store):
    store.add("免費模型下架時改用備用模型，並通知用戶", kind="decision", keywords="fallback")
    store.add("今日天氣晴朗，適合行山", kind="chat")
    res = store.search("免費嘢死咗點算")
    assert res and "下架" in res[0]["content"]


def test_keywords_field_is_searched(store):
    nid, _ = store.add("改用 rsync --partial 續傳", kind="tech", keywords="搬家 斷線 大檔 migrate")
    store.add("rsync 是一個工具", kind="chat")
    assert store.search("搬家斷線")[0]["id"] == nid


def test_kind_weight_prefers_decision(store):
    t = time.time()
    a, _ = store.add("記憶後端選用 Hindsight", kind="news", created_at=t)
    b, _ = store.add("記憶後端選用 Hindsight。", kind="decision", created_at=t)
    ids = [r["id"] for r in store.search("記憶後端")]
    assert ids.index(b) < ids.index(a)


def test_recency_decay_prefers_newer(store):
    now = time.time()
    old, _ = store.add("週報時間改為十點", kind="decision", created_at=now - 400 * 86400)
    new, _ = store.add("週報時間改為十點半", kind="decision", created_at=now)
    assert store.search("週報時間")[0]["id"] == new


def test_expiry_hidden_not_deleted_then_purged(store):
    now = time.time()
    nid, _ = store.add("某新聞：伺服器價格上升", kind="news", created_at=now - 20 * 86400)
    assert store.get(nid)["expires_at"] is not None
    assert store.search("伺服器價格", now=now) == []
    assert store.get(nid) is not None  # search never deletes
    assert store.maintenance(now=now) == 1
    assert store.get(nid) is None


def test_ttl_per_kind(store):
    now = time.time()
    chat, _ = store.add("閒聊", kind="chat", created_at=now)
    dec, _ = store.add("決定用 SQLite", kind="decision", created_at=now)
    assert abs(store.get(chat)["expires_at"] - (now + 90 * 86400)) < 1
    assert store.get(dec)["expires_at"] is None


def test_pinned_never_expires(store):
    nid, _ = store.add("重要新聞", kind="news", pinned=True, created_at=time.time() - 100 * 86400)
    assert store.get(nid)["expires_at"] is None
    assert store.maintenance() == 0


def test_exact_and_near_duplicate(store):
    a, created = store.add("User: 週報幾點\nAssistant: 10:30", kind="chat")
    b, created2 = store.add("User: 週報幾點\nAssistant: 10:30 ", kind="chat")
    assert created and not created2 and a == b
    c, created3 = store.add("User: 週報幾點？\nAssistant: 10:30", kind="chat", dedup_threshold=0.8)
    assert not created3 and c == a


def test_forget_and_list(store):
    a, _ = store.add("決定 A", kind="decision")
    store.add("技術 B", kind="tech")
    assert [n["kind"] for n in store.list(kind="decision")] == ["decision"]
    assert store.forget(a) and store.get(a) is None
    assert not store.forget(a)
    assert store.search("決定") == [] or all(r["id"] != a for r in store.search("決定"))


def test_invalid_kind_rejected(store):
    import pytest
    with pytest.raises(ValueError):
        store.add("x", kind="bogus")


FORGOTTEN_TEXT = "Zorblax backup runs at 03:00 every night on node seven."


def test_update_refuses_forgotten_text(store, store_mod):
    gone, _ = store.add(FORGOTTEN_TEXT, kind="tech")
    other, _ = store.add("Quillon reports are due on Fridays.", kind="tech")
    assert store.forget(gone)
    with pytest.raises(store_mod.RefusedWrite) as exc:
        store.update(other, FORGOTTEN_TEXT)
    from hermind.store_writes import FORGOTTEN_REFUSAL
    assert str(exc.value) == FORGOTTEN_REFUSAL
    with pytest.raises(store_mod.RefusedWrite):
        store.update(other, "  " + FORGOTTEN_TEXT.upper() + " ")        # same normalised digest
    assert store.get(other)["content"] == "Quillon reports are due on Fridays."
    assert store.search("Zorblax") == [] and not store.history(other)


def test_plugin_memory_update_returns_the_forgotten_refusal(provider):
    gone = json.loads(provider.handle_tool_call("memory_remember", {"content": FORGOTTEN_TEXT, "kind": "tech"}))["id"]
    other = json.loads(provider.handle_tool_call(
        "memory_remember", {"content": "Quillon reports are due on Fridays.", "kind": "tech"}))["id"]
    provider.handle_tool_call("memory_forget", {"id": gone})
    out = json.loads(provider.handle_tool_call("memory_update", {"id": other, "content": FORGOTTEN_TEXT}))
    assert out["refused"] and "forgotten" in out["error"]
    assert provider._store.get(other)["content"] == "Quillon reports are due on Fridays."
    assert provider._store.search("Zorblax") == []


def test_search_for_only_very_common_words_still_returns_results_quickly(store):
    with store.conn:
        for i in range(2000):
            store.add(f"Zorblax backup note number {i} runs on node {i * 7}", kind="tech", commit=False)
    for query in ("zorblax", "zorblax backup", "backup note zorblax"):     # every term is in 100 % of the notes
        t0 = time.time()
        hits = store.search(query, limit=5)
        took = time.time() - t0
        assert len(hits) == 5 and all("Zorblax" in h["content"] for h in hits), query
        assert took < 1.0, (query, took)
    assert len(store.search_multi(["zorblax", "backup"], limit=5)) == 5
    # a distinctive term still ranks first, as before
    assert store.search("zorblax node 9359", limit=3)[0]["content"].endswith("on node 9359")


def test_common_words_fallback_scores_one_term_over_a_bounded_number_of_postings(store, monkeypatch):
    """Round 2: the fallback scored the 4 rarest common terms over EVERY posting (25x a normal search on 55,000 notes)."""
    import sys
    ss = sys.modules["hermind.store_search"]
    monkeypatch.setattr(ss, "COMMON_FALLBACK_POSTINGS", 40)
    with store.conn:
        for i in range(500):
            store.add(f"Zorblax backup note number {i} runs on node {i * 7}", kind="tech", commit=False)
    now = time.time()
    n_docs, avg_c, avg_k = store._live_stats(now)
    terms = store._query_terms("zorblax backup")                         # both words are in 100 % of the notes
    bm, kws, matched = store._bm25_multi([terms], n_docs, avg_c, avg_k, now, 10_000, None)[0]
    assert 0 < len(bm) <= 40                                              # the scored rows are bounded, not 500
    newest = {r[0] for r in store.conn.execute("SELECT id FROM notes ORDER BY id DESC LIMIT 40")}
    assert set(bm) <= newest                                              # and they are the newest postings
    hits = store.search("zorblax backup", limit=5)
    assert len(hits) == 5 and all(h["id"] in newest for h in hits)
    # without the fallback (the dedup probe, write_check) nothing is scored at all
    assert store._bm25_multi([terms], n_docs, avg_c, avg_k, now, 10_000, None, rare_fallback=False) == [({}, {}, {})]
    assert store.dedup_candidates("zorblax backup") == []
    assert store.write_check("zorblax backup") == {"similar": [], "possible_conflicts": []}
    # a query with one distinctive term is untouched by all of this
    assert store.search("zorblax node 2093", limit=3)[0]["content"].endswith("on node 2093")
