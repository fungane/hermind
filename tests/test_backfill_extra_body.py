"""tools/backfill_aliases.py: --extra-body is merged into the request (needed to switch a reasoning model's thinking off)."""
import argparse
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_aliases_t", ROOT / "tools" / "backfill_aliases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_extra_body_is_merged_and_optional(monkeypatch):
    mod = _load()
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode()

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=0: (sent.append(json.loads(req.data)), Resp())[1])
    base = dict(model="m", base_url="http://127.0.0.1:9/v1", api_key_env="", batch_size=2, timeout=5.0)
    mod.call_llm(argparse.Namespace(**base, extra_body=""), "hi")
    mod.call_llm(argparse.Namespace(**base, extra_body='{"thinking": {"type": "disabled"}}'), "hi")
    assert "thinking" not in sent[0] and sent[1]["thinking"] == {"type": "disabled"} and sent[1]["model"] == "m"
    assert mod.build_parser().parse_args(["--db", "x", "--base-url", "u", "--model", "m", "--extra-body", "{}"]).extra_body == "{}"
