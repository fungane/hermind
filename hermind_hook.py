#!/usr/bin/env python3
"""Automatic recall for agents that run a command before every prompt (Claude Code's ``UserPromptSubmit`` hook).

    hermind-hook [--db FILE] [--config FILE.json] [--scope NAME]

Reads the hook's JSON from stdin, looks the prompt up in the Hermind database (read-only) and prints the notes that
match as extra context for the model. It uses the same rules as the Hermes plugin's automatic recall: nothing for filler
prompts, a small character budget, notes fenced as data. Pure standard library; usually 0.1 to 0.3 s.

It never blocks and never fails the prompt: on any problem (no database, bad input, an error) it prints nothing and
exits 0. It never writes to the database and never prints note text anywhere but stdout. ``HERMIND_HOOK=off`` disables it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_PROMPT_CHARS = 4000          # the hook gets the whole prompt, pasted files included; only the start is searched


def _hermind_mcp():
    try:
        from . import hermind_mcp  # installed package
    except ImportError:
        import hermind_mcp  # run from a checkout
    return hermind_mcp


def recall(prompt: str, argv: list[str]) -> str:
    """The context block for a prompt, or ''."""
    prompt = (prompt or "").strip()[:MAX_PROMPT_CHARS]
    if not prompt or prompt.startswith("/"):          # empty, or a slash command rather than a question
        return ""
    mcp = _hermind_mcp()
    server, _ns = mcp.make_server(argv)
    db = Path(os.path.expanduser(server.db))
    if not db.exists():                                  # never create a database from a hook
        return ""
    server.write = False
    server.config.setdefault("prefetch_line_chars", 200)    # no memory_get here unless the MCP server is also set up
    try:
        server.open()
        return server._provider.prefetch(prompt, session_id="hook") or ""
    finally:
        server.close()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip())
        return 0
    if os.environ.get("HERMIND_HOOK", "").strip().lower() in ("0", "off", "false", "no"):
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict) or data.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
            return 0
        block = recall(str(data.get("prompt") or ""), argv)
    except SystemExit:
        return 0
    except Exception as exc:                              # a hook must never get in the way of the prompt
        if os.environ.get("HERMIND_HOOK_DEBUG"):
            print(f"hermind-hook: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 0
    if block:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                            "additionalContext": block}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
