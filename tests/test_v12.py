"""platform boilerplate never drives recall, extracted near-copies refresh the existing note, synonym
expansion respects filler / generic words, stricter extraction prompt."""
import json

import pytest


@pytest.mark.parametrize("raw,want", [
    ("What do you see in this image?\n\n[Image attached at: /home/user/.hermes/cache/images/img_1.jpg]", ""),
    ("What do you see in this image? @image:/home/user/.hermes/images/upload_x.png", ""),
    ("[IMPORTANT: Background process proc_1 completed normally (exit code 0).\nCommand: ls]", ""),
    ("The document is too large or its size could not be verified. Maximum: 20 MB.", ""),
    ("為什麼 進度列 不顯示\n\n[Image attached at: /tmp/a.png]", "為什麼 進度列 不顯示"),
    ("裝好了 [1 image]", "裝好了"),
    ("Nebula 閃退", "Nebula 閃退"),
])
def test_clean_prompt(pkg, raw, want):
    from hermind.screen import clean_prompt
    assert clean_prompt(raw) == want


def test_prefetch_ignores_image_boilerplate(provider):
    provider.handle_tool_call("memory_remember", {"content": "Hermes cache 圖片放在 ~/.hermes/cache/images",
                                                  "keywords": "hermes cache images image"})
    assert provider.prefetch("What do you see in this image?\n\n[Image attached at: /home/user/.hermes/cache/images/a.jpg]") == ""
    assert "cache" in provider.prefetch("hermes cache images 放在哪")


def _fact(text, aliases=("a", "b")):
    return {"text": text, "kind": "tech", "aliases": list(aliases)}


def _active(provider):
    return provider._store.conn.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0]


class _FakeLLM:
    capture_extract = True
    rerank_on = False
    last_usage = {}

    def __init__(self, facts):
        self.facts = facts

    def extract_facts(self, user, assistant):
        return self.facts


def test_extracted_near_copy_refreshes_existing_note(provider):
    old = "Atlas 團隊的匯報練習：講者語速約每分鐘 120 字，600 字講了 5 分 10 秒"
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": old, "kind": "tech",
                                                                   "keywords": "匯報 語速"}))["id"]
    before = _active(provider)
    new = "Atlas 團隊的匯報練習：講者語速約每分鐘 120 字（600 字講了 5 分 10 秒，已計入換頁），日後按此估算"
    provider._llm = _FakeLLM([_fact(new, ("念稿速度", "speech rate"))])
    provider._extract_and_store("u", "a", "chunk", "s1")
    assert _active(provider) == before
    got = provider._store.get(nid)
    assert got["content"] == new and "speech" in got["keywords"] and got["proof_count"] >= 2
    assert provider._store.history(nid)            # old wording kept as history


def test_shorter_restatement_does_not_overwrite(provider):
    long = "Atlas 有兩份路由器設定備份：~/router-conf（換光纖前）和 ~/router-conf2（升級韌體前），重設前要先抄低"
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": long, "keywords": "備份 atlas 路由器"}))["id"]
    provider._llm = _FakeLLM([_fact("Atlas 有兩份路由器設定備份：~/router-conf 和 ~/router-conf2")])
    provider._extract_and_store("u", "a", "chunk", "s1")
    assert provider._store.get(nid)["content"] == long


def test_unrelated_fact_is_added(provider):
    provider.handle_tool_call("memory_remember", {"content": "VPS 有 3.8GB RAM", "keywords": "vps ram"})
    before = _active(provider)
    provider._llm = _FakeLLM([_fact("用戶的雅思寫作目標是剛好 6.0")])
    provider._extract_and_store("u", "a", "chunk", "s1")
    assert _active(provider) == before + 1


def test_pinned_note_never_rewritten(provider):
    txt = "用戶的主要頻道是 Telegram，WeChat 只收純文字副本"
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": txt, "pinned": True,
                                                                   "keywords": "telegram wechat"}))["id"]
    provider._llm = _FakeLLM([_fact("用戶的主要頻道是 Telegram，WeChat 只收純文字副本，不支援 markdown 格式")])
    provider._extract_and_store("u", "a", "chunk", "s1")
    assert provider._store.get(nid)["content"] == txt


def test_synonyms_of_filler_and_generic_words_are_damped(store_mod):
    from hermind.textnorm import query_term_factor, GENERIC_FACTOR
    q = "想問一下oh my hermes這個東西你會推薦我去使用麼給我原因和理由"
    assert query_term_factor("oh my hermes", q) == 1.0
    assert query_term_factor("推薦", q) == GENERIC_FACTOR
    assert query_term_factor("原因", q) == 0.0          # only inside the filler phrase 原因和理由


def test_extraction_prompt_defaults_to_nothing(pkg):
    import importlib
    llm = importlib.import_module("hermind.llm")
    p = llm.LLMClient.FACTS_PROMPT
    assert "return an empty list unless" in p and "language the USER" in p
    p.format(turn="x")   # braces stay valid for str.format


def test_extracted_fact_that_was_forgotten_does_not_rewrite_a_twin(provider):
    gone = "Zorblax backup runs at 03:00 every night on node seven today."
    twin = "Zorblax backup runs at 03:00 every night on node seven."
    gid = json.loads(provider.handle_tool_call("memory_remember", {"content": gone, "kind": "tech"}))["id"]
    nid = json.loads(provider.handle_tool_call("memory_remember", {"content": twin, "kind": "tech"}))["id"]
    provider.handle_tool_call("memory_forget", {"id": gid})
    provider._llm = _FakeLLM([_fact(gone)])
    provider._extract_and_store("u", "a", "chunk", "s1")          # the refusal is a normal skip, not an error
    assert provider._store.get(nid)["content"] == twin
    assert all(h["content"] != gone for h in provider._store.search("seven today"))


def test_builtin_memory_replace_with_forgotten_text_is_skipped(provider):
    gone = "Zorblax backup runs at 03:00 every night on node seven."
    provider.on_memory_write("add", "memory", "Quillon reports are due on Fridays.")
    provider.on_memory_write("add", "memory", gone)
    gid = provider._store.find_by_content(gone)
    provider._store.forget(gid)
    provider.on_memory_write("replace", "memory", gone, {"previous_content": "Quillon reports are due on Fridays."})
    assert provider._store.find_by_content(gone) is None            # not resurrected, no exception
    assert provider._store.find_by_content("Quillon reports are due on Fridays.") is not None
