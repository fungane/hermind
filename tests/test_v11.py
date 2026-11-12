"""masked-credential refusal, query filler / generic-word weighting, faster decay of progress snapshots,
vector-fused automatic recall."""
import json

import pytest

# Masked forms as Hermes' egress scrub produces them (prefix + "..." + last 4, or "***"); built at runtime.
MASKED = [
    "key " + "sk" + "-pro...IiHh",
    "h" + "f_Zz9...OoNn",
    "n" + "pm_Ab...UvWx",
    "gl" + "pat-...l6Mn",
    "s" + "k_liv...5Kl6",
    "postgres://admin:" + "***" + "@db.example.com:5432/prod",
    "aws_secret" + "_access_key=" + "***",
    "DEEPSEEK_API" + "_KEY=" + "***",
]
NOT_MASKED = [
    "Nebu...la is fine", "下載中...完成", "三個安裝檔正在上傳...", "see https://x.com/a...b",
    "password is stored in Bitwarden", "the sk-learn library", "ratio 3...5 is fine",
]


@pytest.mark.parametrize("text", MASKED)
def test_masked_credentials_detected(pkg, text):
    from hermind.screen import looks_masked_secret
    assert looks_masked_secret(text)


@pytest.mark.parametrize("text", NOT_MASKED)
def test_masked_screen_false_positives(pkg, text):
    from hermind.screen import looks_masked_secret
    assert looks_masked_secret(text) is None


def test_remember_refuses_masked_credential(provider):
    out = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "deploy key is " + "sk" + "-pro...IiHh", "keywords": "deploy key"}))
    assert out.get("refused") and "masked" in out["error"]
    ok = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "the deploy key lives in the vault, not in notes", "keywords": "deploy key vault"}))
    assert ok.get("created")


def test_update_refuses_masked_credential(provider):
    nid = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "database connection is configured in .env", "keywords": "database env"}))["id"]
    out = json.loads(provider.handle_tool_call("memory_update", {
        "id": nid, "content": "db is postgres://admin:" + "***" + "@h/app"}))
    assert out.get("refused")


def test_filler_is_dropped_and_generic_words_down_weighted(store_mod):
    from hermind.textnorm import query_token_weights, normalize
    w = query_token_weights("重新幫我測試一下memory然後出錯的話幫我排查一下問題在哪裡謝謝")
    assert w["memory"] > 1.0
    for filler in ("謝謝", "幫我", "一下", "然後", "重新"):
        assert w.get(normalize(filler), 0.0) == 0.0
    assert 0 < w[normalize("排查")] < 0.5
    # a query made only of filler keeps its plain weights instead of becoming empty
    assert query_token_weights("謝謝")[normalize("謝謝")] > 0


def test_topic_beats_politeness_words(store):
    store.add("用戶的導師是林謝明博士", kind="state", keywords="導師 林謝明")
    store.add("Nebula 閃退問題已修好", kind="tech", keywords="nebula crash 閃退")
    top = store.search("幫我睇下 nebula 閃退 謝謝", limit=1)
    assert top and "Nebula" in top[0]["content"]


def test_progress_snapshot_decays_faster(store_mod):
    from hermind.store_consolidate import ConsolidateMixin
    DAY = 86400.0
    row = lambda content: {"kind": "state", "pinned": 0, "last_access": None, "event_at": None,
                           "created_at": 0.0, "content": content}
    now = 60 * DAY
    progress = ConsolidateMixin._idle_factor(row("用戶正在排查遊戲閃退，下一步是重開"), now)
    fact = ConsolidateMixin._idle_factor(row("VPS 有 3.8GB RAM"), now)
    assert progress < fact
    assert ConsolidateMixin._idle_factor(row("用戶正在排查"), 0.0) == 1.0


class _FakeVec:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def available(self):
        return True

    def close(self):
        pass

    def index_missing(self, store, limit=32):
        raise AssertionError("prefetch must not embed new notes")

    def search(self, q, timeout=None):
        self.calls.append(timeout)
        return self.hits


def test_prefetch_fuses_vector_hits(provider):
    a = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "memory 插件 Hermind 1.0.1 已部署並生效", "keywords": "hermind memory 部署"}))["id"]
    provider.handle_tool_call("memory_remember", {"content": "遊戲伺服器排查問題：先看 server.log",
                                                  "keywords": "排查 問題 server log"})
    provider._vec = _FakeVec([(a, 0.9)])
    out = provider.prefetch("幫我排查一下 memory 問題")
    assert "Hermind 1.0.1" in out
    assert provider._vec.calls and provider._vec.calls[0] is not None   # bounded timeout


def test_prefetch_survives_vector_failure(provider):
    provider.handle_tool_call("memory_remember", {"content": "Telegram 是主要頻道", "keywords": "telegram 頻道"})
    v = _FakeVec(None)
    provider._vec = v
    assert "Telegram" in provider.prefetch("telegram 頻道")


def test_topical_weight_gate(pkg):
    from hermind.textnorm import topical_weight
    for q in ("謝謝", "點樣", "繼續", "你覺得要怎麼修就怎麼修謝謝"):
        assert topical_weight(q) < 1.0, q
    for q in ("VPS", "測試memory", "Nebula 閃退", "修好未"):
        assert topical_weight(q) >= 1.0, q


def test_prefetch_skips_topicless_prompt(provider):
    provider.handle_tool_call("memory_remember", {"content": "用戶覺得培訓內容太籠統", "keywords": "覺得 培訓"})
    assert provider.prefetch("你覺得要怎麼修就怎麼修謝謝") == ""
    assert "培訓" in provider.prefetch("培訓 內容")


def test_future_created_at_becomes_event_time(store):
    import time
    future = time.time() + 400 * 86400
    nid, _ = store.add("a trial is planned to start later", kind="chat", created_at=future)
    r = store.conn.execute("SELECT created_at, event_at FROM notes WHERE id=?", (nid,)).fetchone()
    assert r["created_at"] <= time.time() + 1 and abs(r["event_at"] - future) < 1
