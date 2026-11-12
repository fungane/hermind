import time


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
