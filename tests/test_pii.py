"""Personal-data redaction before text goes to an LLM endpoint (pii.py + the llm.py hook). All values are made up."""

import json

import pii
from llm import LLMClient


def test_detects_each_kind():
    assert pii.find_pii("mail me at jane.doe+x@example.co.uk please") == ["email"]
    assert pii.find_pii("HKID A123456(3)") == ["id"]
    assert pii.find_pii("身份證 A1234563") == ["id"]
    assert pii.find_pii("身份證號 11010519491231002X") == ["id"]
    assert pii.find_pii("card 4111 1111 1111 1111") == ["card"]
    assert pii.find_pii("call +852 9123 4567") == ["phone"]
    assert pii.find_pii("電話：9123 4567") == ["phone"]
    assert pii.find_pii("手機 13812345678") == ["phone"]
    assert pii.find_pii("WhatsApp +44 7700 900123") == ["phone"]


def test_ordinary_numbers_and_ids_are_not_flagged():
    for text in ("order 12345678 shipped", "version 2026-07-28", "A123456(9) is not a valid check digit",
                 "card 4111 1111 1111 1112", "11010519491231002Y", "tel aviv is nice", "port 8080 and 9123 items",
                 "build 20261107123456", "see issue #4417 on 2026-11-05"):
        assert pii.find_pii(text) == [], text


def test_redact_replaces_and_counts_and_keeps_the_rest():
    text = "Za (za@example.com, 電話 91234567) holds card 4111111111111111."
    out, counts = pii.redact_pii(text)
    assert out == "Za ([email], 電話 [phone]) holds card [card]."
    assert counts == {"email": 1, "phone": 1, "card": 1}
    assert pii.redact_pii("nothing personal here") == ("nothing personal here", {})


def test_fullwidth_and_zero_width_tricks_do_not_hide_an_email():
    out, _ = pii.redact_pii("ａ\u200bb@ｅxample.com")  # public-check: ok (fullwidth look-alike on purpose)
    assert "example" not in out and "[email]" in out


def test_custom_patterns_and_bad_pattern_ignored():
    out, counts = pii.redact_pii("project codename ORCHID-7 ships", extra=[r"ORCHID-\d", "(unclosed"])
    assert out == "project codename [custom] ships" and counts == {"custom": 1}
    assert pii.find_pii("x", extra=["(unclosed"]) == []


def _client(monkeypatch, **cfg):
    sent = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake(req, timeout=0):
        sent.append(json.loads(req.data)["messages"][0]["content"])
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return LLMClient({"base_url": "http://x/v1", "model": "m", **cfg}), sent


def test_llm_default_redacts_before_sending(monkeypatch):
    c, sent = _client(monkeypatch)
    assert c.chat("Rank: Za's mail is za@example.com, card 4111111111111111") == "ok"
    assert sent == ["Rank: Za's mail is [email], card [card]"] and c.pii_redacted == 1


def test_llm_block_mode_skips_the_call(monkeypatch):
    c, sent = _client(monkeypatch, pii="block")
    assert c.chat("mail za@example.com") is None and sent == [] and c.pii_blocked == 1
    assert c.chat("nothing personal") == "ok" and sent == ["nothing personal"]


def test_llm_off_mode_and_unknown_mode(monkeypatch):
    c, sent = _client(monkeypatch, pii="off")
    c.chat("mail za@example.com")
    assert sent == ["mail za@example.com"]
    c2, sent2 = _client(monkeypatch, pii="nonsense")             # unknown value falls back to the safe default
    c2.chat("mail za@example.com")
    assert sent2 == ["mail [email]"]


def test_llm_custom_patterns(monkeypatch):
    c, sent = _client(monkeypatch, redact_patterns=[r"ORCHID-\d"])
    c.chat("see ORCHID-7")
    assert sent == ["see [custom]"]


def test_ssh_targets_timestamps_and_dois_are_not_pii():
    for text in ("ssh ubuntu@203.0.113.7 works", "export/20260115-093012/ folder", "DOI 10.5555/press/9780306406157.001.0001",
                 "hermind-pre-2.0.0-20250316-1845. Verify", "git@github.com:example/hermind.git is a remote"):  # public-check: ok (a git remote, not a mailbox)
        found = pii.find_pii(text)
        assert found == [] or text.startswith("git@"), (text, found)    # a real host name still counts as an address
    assert pii.find_pii("write to jane@example.org") == ["email"]


def test_card_formats_still_found():
    for text in ("4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111", "3782 822463 10005", "378282246310005"):
        assert pii.find_pii(text) == ["card"], text
