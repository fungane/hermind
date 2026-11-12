"""`hermind import --help` and `--readers` explain the readers, --include-removed and the By source section."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402

NEW = ("claude-code", "chatgpt-export", "claude-export", "instructions", "paste")


def test_help_explains_the_new_readers_the_option_and_the_by_source_section(capsys):
    try:
        hermind_cli.main(["import", "--help"])
    except SystemExit:
        pass
    text = capsys.readouterr().out
    for name in NEW:
        assert name in text
    for phrase in ("--include-removed", "By source", "not published", "never opened", "--from paste", "idle clock"):
        assert phrase in text


def test_readers_lists_the_new_readers(capsys):
    assert hermind_cli.main(["import", "--readers", "--json"]) == 0
    names = [r["name"] for r in json.loads(capsys.readouterr().out)["readers"]]
    assert set(NEW) <= set(names)
    assert hermind_cli.main(["import", "--readers"]) == 0
    text = capsys.readouterr().out
    assert all(n in text for n in NEW)
