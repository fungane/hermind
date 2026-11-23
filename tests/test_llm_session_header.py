"""Check outgoing request metadata without connecting to a model or a server."""
import io
import urllib.request

import pytest

from llm import LLMClient, USER_AGENT


@pytest.mark.parametrize("base,allowed", [
    ("https://opencode.ai/v1", True), ("https://OpenCode.AI:443/v1", True),
    ("http://opencode.ai/v1", False), ("https://relay.example/opencode.ai", False),
    ("https://opencode.ai.example/v1", False), ("https://sub.opencode.ai/v1", False),
])
def test_relay_session_header(monkeypatch, base, allowed):
    captured = []

    def answer(request, timeout):
        captured.append(request)
        return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    client = LLMClient({"base_url": base, "model": "stub"})
    assert client.chat("A fictional note") == "ok"
    req, = captured
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["user-agent"] == USER_AGENT
    assert headers.get("x-opencode-session") == (client.session_id if allowed else None)
    # urllib must not copy the identifier onto an automatic redirect.
    assert "x-opencode-session" not in {k.lower() for k in req.headers}

@pytest.mark.parametrize("caller", ["client", "summaries", "aliases"])
def test_auth_only_on_initial_request(monkeypatch, caller):
    from argparse import Namespace
    from pathlib import Path
    from urllib.request import HTTPRedirectHandler

    captured = []

    def answer(request, timeout):
        captured.append(request)
        return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    monkeypatch.setenv("FIXTURE_KEY", "fixture-only")
    cfg = dict(base_url="https://relay.example/v1", model="stub", api_key_env="FIXTURE_KEY",
               batch_size=1, timeout=1)
    if caller == "client":
        assert LLMClient(cfg).chat("Fictional note") == "ok"
    else:
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "hermind" / "tools"))
        import backfill_aliases
        import build_summaries
        call = build_summaries.call_usage if caller == "summaries" else backfill_aliases.call_llm
        assert call(Namespace(**cfg), "Fictional note") == "ok"
    req, = captured
    assert req.get_header("Authorization") == "Bearer fixture-only"
    redirected = HTTPRedirectHandler().redirect_request(
        req, None, 303, "See Other", {}, "https://other.example/result")
    assert not redirected.has_header("Authorization")
