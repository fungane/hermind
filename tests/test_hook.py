"""Automatic recall hook (Claude Code UserPromptSubmit) and `hermind install-hook`."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
HOOK = str(ROOT / "hermind_hook.py")
CLI = str(ROOT / "hermind_cli.py")


@pytest.fixture
def db(tmp_path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    path = str(tmp_path / "hook.db")
    s = DecisionStore(path)
    s.add("The staging database is migrated every Monday morning by the release script, after the nightly backup finished.",
          "decision", "staging database migration monday release script backup")
    s.add("Alice prefers short written updates instead of video calls.", "preference", "alice written updates calls")
    s.close()
    return path


def hook(prompt, *args, db_path=None, env=None, event="UserPromptSubmit", raw=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("HERMIND_", "HERMES_"))}
    e.update(env or {})
    payload = raw if raw is not None else json.dumps({"hook_event_name": event, "prompt": prompt})
    a = [sys.executable, HOOK, *(["--db", db_path] if db_path else []), *args]
    return subprocess.run(a, input=payload, capture_output=True, text=True, env=e, timeout=60)


def context(r):
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""


def test_hook_returns_matching_notes_as_fenced_context(db):
    r = hook("when do we migrate the staging database", db_path=db)
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    ctx = out["additionalContext"]
    assert ctx.startswith("<hermind-context>") and ctx.rstrip().endswith("</hermind-context>")
    assert "migrated every Monday" in ctx and "Alice" not in ctx
    assert len(ctx) <= 1500


@pytest.mark.parametrize("prompt", ["", "ok", "thanks", "/clear", "/deploy staging", "   ", "the a an"])
def test_hook_stays_quiet_for_prompts_with_nothing_to_look_up(db, prompt):
    assert context(hook(prompt, db_path=db)) == ""


def test_hook_never_fails_the_prompt(db, tmp_path):
    assert hook("x", raw="not json", db_path=db).returncode == 0
    assert hook("x", raw="[1,2]", db_path=db).returncode == 0
    assert context(hook("when do we migrate the staging database", db_path=db, event="Stop")) == ""   # other events ignored
    missing = tmp_path / "none" / "x.db"
    r = hook("when do we migrate the staging database", db_path=str(missing))
    assert r.returncode == 0 and r.stdout == "" and not missing.exists() and not missing.parent.exists()   # no db is created
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 50)
    r = hook("when do we migrate the staging database", db_path=str(junk))
    assert r.returncode == 0 and r.stdout == ""
    assert context(hook("when do we migrate the staging database", db_path=db, env={"HERMIND_HOOK": "off"})) == ""


def test_hook_is_read_only(db):
    before = Path(db).read_bytes()
    for _ in range(3):
        assert "migrated" in context(hook("when do we migrate the staging database", db_path=db))
    assert Path(db).read_bytes() == before
    con = sqlite3.connect(db)
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_log'").fetchone() or \
        con.execute("SELECT count(*) FROM usage_log").fetchone()[0] == 0
    assert con.execute("SELECT sum(shown_count) FROM notes").fetchone()[0] in (0, None)
    con.close()


def test_hook_does_not_search_private_text(db):
    assert context(hook("<private>staging database migration</private>", db_path=db)) == ""
    ctx = context(hook("<private>alice written</private> when do we migrate the staging database", db_path=db))
    assert "migrated" in ctx and "Alice" not in ctx


def test_hook_reads_only_the_start_of_a_huge_prompt(db):
    big = "when do we migrate the staging database " + ("filler words " * 200000)
    assert "migrated" in context(hook(big, db_path=db))


def test_hook_honours_the_database_env_var(db):
    assert "migrated" in context(hook("when do we migrate the staging database", env={"HERMIND_DB": db}))


# -- install-hook ---------------------------------------------------------------------------------------------------


def cli(*args, home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMIND_", "HERMES_"))}
    env["HOME"] = env["USERPROFILE"] = str(home)
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=env, timeout=120)


OTHER = {"model": "x", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo other"}]}],
                                 "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}


def ours(settings):
    return [h for g in settings.get("hooks", {}).get("UserPromptSubmit", []) for h in g["hooks"] if "hermind_hook" in h["command"]]


def test_install_hook_preview_changes_nothing_and_probes(db, tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps(OTHER))
    before = p.read_bytes()
    d = json.loads(cli("install-hook", "--db", db, "--json", home=tmp_path).stdout)
    assert d["ok"] and d["written"] is False and p.read_bytes() == before
    assert d["probe"]["ran"] is True and d["probe"]["returned_valid_context"] is False   # the probe prompt matches no note here
    assert "hermind_hook.py" in d["command"] and "--db" in d["command"]
    assert not list(p.parent.glob("*.bak-hermind-*"))


def test_install_hook_write_merges_backs_up_and_is_idempotent(db, tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps(OTHER))
    d1 = json.loads(cli("install-hook", "--db", db, "--write", "--json", home=tmp_path).stdout)
    d2 = json.loads(cli("install-hook", "--db", db, "--write", "--timeout", "7", "--json", home=tmp_path).stdout)
    s = json.loads(p.read_text())
    assert d1["written"] and d2["replaced_existing_entries"] == 1
    assert len(ours(s)) == 1 and ours(s)[0]["timeout"] == 7                      # replaced, not duplicated
    assert s["model"] == "x" and s["hooks"]["Stop"] == OTHER["hooks"]["Stop"]
    assert s["hooks"]["UserPromptSubmit"][0] == OTHER["hooks"]["UserPromptSubmit"][0]   # other tools' hooks untouched
    backups = sorted(p.parent.glob("settings.json.bak-hermind-*"))
    assert len(backups) == 2 and json.loads(backups[0].read_text()) == OTHER      # the first backup is the original
    if sys.platform != "win32":                                                   # Windows has no Unix permission bits (a new file reports 0o666)
        assert all((b.stat().st_mode & 0o777) == 0o600 for b in backups)


def test_install_hook_remove_leaves_everything_else(db, tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps(OTHER))
    cli("install-hook", "--db", db, "--write", home=tmp_path)
    d = json.loads(cli("install-hook", "--remove", "--write", "--json", home=tmp_path).stdout)
    assert d["ok"] and d["replaced_existing_entries"] == 1 and json.loads(p.read_text()) == OTHER
    # with nothing else configured the empty containers disappear too
    q = tmp_path / "other" / "settings.json"
    cli("install-hook", "--db", db, "--settings", str(q), "--write", home=tmp_path)
    assert ours(json.loads(q.read_text()))
    cli("install-hook", "--remove", "--settings", str(q), "--write", home=tmp_path)
    assert json.loads(q.read_text()) == {}


def test_install_hook_refuses_unreadable_or_odd_settings(db, tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text("{oops")
    r = cli("install-hook", "--db", db, "--write", home=tmp_path)
    assert r.returncode == 2 and p.read_text() == "{oops" and not list(p.parent.glob("*.bak-*"))
    p.write_text(json.dumps({"hooks": {"UserPromptSubmit": "nope"}}))
    r = cli("install-hook", "--db", db, "--write", home=tmp_path)
    assert r.returncode == 2 and json.loads(p.read_text()) == {"hooks": {"UserPromptSubmit": "nope"}}


def test_install_hook_creates_settings_when_missing(db, tmp_path):
    d = json.loads(cli("install-hook", "--db", db, "--write", "--json", home=tmp_path).stdout)
    p = tmp_path / ".claude" / "settings.json"
    assert d["written"] and "backup" not in d and len(ours(json.loads(p.read_text()))) == 1


def test_install_hook_command_runs_like_claude_code_does(db, tmp_path):
    d = json.loads(cli("install-hook", "--db", db, "--json", home=tmp_path).stdout)
    r = subprocess.run(d["command"], shell=True, input=json.dumps({"hook_event_name": "UserPromptSubmit",
                       "prompt": "when do we migrate the staging database"}), capture_output=True, text=True, timeout=60)
    assert "migrated every Monday" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_install_hook_quotes_paths_with_spaces(tmp_path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    odd = tmp_path / "my notes"
    odd.mkdir()
    s = DecisionStore(str(odd / "m y.db"))
    s.add("The printer in room 4 uses the blue toner cartridge model 88.", "tech", "printer room toner cartridge")
    s.close()
    d = json.loads(cli("install-hook", "--db", str(odd / "m y.db"), "--json", home=tmp_path).stdout)
    r = subprocess.run(d["command"], shell=True, input=json.dumps({"prompt": "which toner cartridge does the printer use"}),
                       capture_output=True, text=True, timeout=60)
    assert "blue toner" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_survives_a_console_that_cannot_encode_the_recall(tmp_path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    path = str(tmp_path / "cjk.db")
    s = DecisionStore(path)
    s.add("Zorblax 備份每日凌晨三點在第七節點執行，保留七日。", "tech", "zorblax backup nightly")
    s.close()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("HERMIND_", "HERMES_"))}
    e.update(PYTHONIOENCODING="ascii", HOME=str(tmp_path))
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "when does the zorblax backup run?"})
    r = subprocess.run([sys.executable, HOOK, "--db", path], input=payload.encode("ascii"), capture_output=True, env=e, timeout=60)
    assert r.returncode == 0 and r.stderr == b""
    ctx = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert "備份每日凌晨三點" in ctx
    # a prompt in Chinese reaches the hook as UTF-8 bytes even though the console is ASCII
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "備份幾點執行 zorblax"}, ensure_ascii=False)
    r = subprocess.run([sys.executable, HOOK, "--db", path], input=payload.encode("utf-8"), capture_output=True, env=e, timeout=60)
    assert r.returncode == 0 and "備份每日凌晨三點" in json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
