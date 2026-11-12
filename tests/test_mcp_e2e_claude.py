"""End-to-end: the real Claude Code CLI uses the server (tiny API cost, needs a logged-in `claude`).
Opt-in:  HERMIND_E2E_CLAUDE=1 python -m pytest tests/test_mcp_e2e_claude.py
Uses --mcp-config <temp file> --strict-mcp-config and a temporary database; no settings are changed."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (os.environ.get("HERMIND_E2E_CLAUDE") == "1" and shutil.which("claude")),
                                reason="set HERMIND_E2E_CLAUDE=1 (needs the claude CLI)")


def _run(tmp_path, prompt, server_args, db, allowed):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"hermind": {
        "command": sys.executable, "args": [str(ROOT / "hermind_mcp.py"), *server_args],
        "env": {"HERMIND_DB": str(db)}}}}))
    p = subprocess.run(["claude", "-p", prompt, "--mcp-config", str(cfg), "--strict-mcp-config", "--max-turns", "5",
                        "--model", "haiku", "--allowedTools", allowed, "--output-format", "stream-json", "--verbose",
                        "--no-session-persistence", "--disable-slash-commands"],
                       capture_output=True, text=True, timeout=240, cwd=str(tmp_path), stdin=subprocess.DEVNULL)
    events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    used = [c["name"] for e in events if e.get("type") == "assistant" for c in e["message"]["content"]
            if c["type"] == "tool_use"]
    result = next(e for e in events if e.get("type") == "result")
    return init, used, result


def test_claude_code_reads_notes_read_only(tmp_path, store_mod):
    db = tmp_path / "n.db"
    s = store_mod.DecisionStore(str(db))
    s.add("The staging cluster codename is Zephyr-Marmot-42 and it runs in region eu-north.", kind="tech",
          keywords="staging cluster codename environment name")
    s.close()
    init, used, result = _run(tmp_path, "What is the codename of the staging cluster? Use the hermind memory tools, "
                              "answer in one short sentence.", [], db, "mcp__hermind__memory_search,mcp__hermind__memory_get")
    assert [m["status"] for m in init["mcp_servers"] if m["name"] == "hermind"] == ["connected"]
    assert not any("remember" in t for t in init["tools"] if "hermind" in t)
    assert "mcp__hermind__memory_search" in used
    assert "Zephyr-Marmot-42" in result["result"]


def test_claude_code_writes_notes_when_enabled(tmp_path):
    db = tmp_path / "w.db"
    init, used, result = _run(tmp_path, "Save this with the hermind memory_remember tool: 'The lab printer is named "
                              "Quokka and sits on floor 3.' Then reply done.", ["--write"], db,
                              "mcp__hermind__memory_search,mcp__hermind__memory_remember")
    assert "mcp__hermind__memory_remember" in used
    row = sqlite3.connect(db).execute("SELECT source_session, content FROM notes").fetchone()
    assert row[0] == "mcp:claude-code" and "Quokka" in row[1]
