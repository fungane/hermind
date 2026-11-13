"""hermind_cli.py: doctor, selftest, install-hermes (dry run, real, rollback), config snippets. Everything runs against a fake HOME."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
CLI = str(ROOT / "hermind_cli.py")


def _native(path):
    """A path as the CLI prints it: Path() turns "/" into the platform's separator (nothing changes on Linux and macOS)."""
    return str(Path(path))


def _in_json(text):
    """How a string reads inside JSON (or JSON-quoted YAML/TOML) text: a Windows path has its backslashes doubled there."""
    return json.dumps(text, ensure_ascii=False)[1:-1]


def run(*args, home, extra_env=None, check=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMIND_", "HERMES_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), **(extra_env or {}))
    r = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=env, timeout=180)
    if check is not None:
        assert r.returncode == check, (r.returncode, r.stdout, r.stderr)
    return r


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".hermes").mkdir(parents=True)
    return h


def _seed_db(path):
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    s = DecisionStore(str(path))
    s.add("The deploy window is Tuesday morning.", "decision", "deploy window tuesday")
    s.close()


def test_runtime_files_match_deploy_sh():
    sys.path.insert(0, str(ROOT))
    import hermind_cli
    text = (ROOT / "tools" / "deploy.sh").read_text()
    listed = set(re.search(r"for f in (.*?); do", text).group(1).split())
    in_sh = {f for f in listed if f != "store*.py"}
    assert 'cp "$SRC"/../README.md "$D/"' in text                  # README.md sits at the top of the checkout
    assert in_sh | {"README.md"} == set(hermind_cli.RUNTIME_FILES)
    assert "store.py" in hermind_cli.runtime_files() and "store_schema.py" in hermind_cli.runtime_files()
    assert all(hermind_cli.runtime_source(f).exists() for f in hermind_cli.runtime_files())
    assert hermind_cli.runtime_source("README.md") == REPO / "README.md"


def test_version_flag(home):
    assert run("--version", home=home, check=0).stdout.strip().startswith("hermind 1.")


def test_doctor_reports_profiles_and_flags_an_old_plugin(home):
    _seed_db(home / ".hermes" / "decision_memory.db")
    old = home / ".hermes" / "profiles" / "friend" / "plugins" / "hermind"
    old.mkdir(parents=True)
    (old / "plugin.yaml").write_text("name: hermind\nversion: 1.2.0\n")
    d = json.loads(run("doctor", "--json", home=home, check=0).stdout)
    by = {p["name"]: p for p in d["hermes_profiles"]}
    assert by["default"]["db"]["state"] == "plain" and by["default"]["db"]["notes"] == 1
    assert by["friend"]["plugin_version"] == "1.2.0" and by["friend"]["db"]["state"] == "missing"
    assert any("friend" in s and "install-hermes" in s for s in d["next_steps"])
    assert d["ok"] is True and d["python"]["version"]


def test_doctor_flags_an_encrypted_database_without_a_key(home):
    (home / ".hermes" / "decision_memory.db").write_bytes(os.urandom(4096))        # not a plain SQLite header
    r = run("doctor", "--json", home=home)
    d = json.loads(r.stdout)
    assert r.returncode == 1 and d["ok"] is False
    assert any("encrypted" in p and "HERMIND_DB_KEY" in p for p in d["problems"])


def test_doctor_human_output(home):
    r = run("doctor", home=home, check=0)
    assert "hermes profile default" not in r.stdout or "plugin" in r.stdout
    assert "selftest" in r.stdout


def test_selftest_passes_on_a_clean_machine(home):
    d = json.loads(run("selftest", "--json", home=home, check=0).stdout)
    assert d["ok"] is True
    assert {c["check"] for c in d["checks"]} >= {"initialize", "write (--write)", "search finds the note",
                                                 "write refused without --write", "2026-07-28 server/discover"}


def test_install_dry_run_changes_nothing(home):
    r = run("install-hermes", "--dry-run", "--json", home=home, check=0)
    d = json.loads(r.stdout)
    assert d["ok"] and d["dry_run"] and any("copy" in s for s in d["steps"])
    assert not (home / ".hermes" / "plugins").exists() and not (home / ".hermes" / "backups").exists()


def test_install_real_then_rollback(home):
    h = home / ".hermes"
    _seed_db(h / "decision_memory.db")
    old = h / "plugins" / "hermind"
    old.mkdir(parents=True)
    (old / "plugin.yaml").write_text("name: hermind\nversion: 1.2.0\n")
    (old / "marker.txt").write_text("old plugin")
    d = json.loads(run("install-hermes", "--json", home=home, check=0).stdout)
    assert d["ok"], d
    assert d["from_version"] == "1.2.0" and d["to_version"].startswith("1.")
    assert d["verify"]["files_identical"] and d["verify"]["opens_database_copy"] and d["verify"]["active_notes"] == 1
    assert (old / "pii.py").exists() and (old / "dbdriver.py").exists() and (old / "tools" / "backup.py").exists()
    backup = Path(d["plugin_backup"])
    assert (backup / "marker.txt").read_text() == "old plugin"
    assert list((h / "backups" / "db").glob("*.db")), "database backup missing"
    assert any("restart" in n for n in d["next"]) and "--rollback" in d["rollback"]
    # the live database file itself was not rewritten
    assert (h / "decision_memory.db").stat().st_size > 0
    r = json.loads(run("install-hermes", "--rollback", str(backup), "--json", home=home, check=0).stdout)
    assert r["ok"] and r["restored_version"] == "1.2.0"
    assert (old / "marker.txt").exists() and not (old / "pii.py").exists()
    assert Path(r["replaced_kept_at"]).exists()                    # the 1.4 plugin is kept aside, not destroyed
    r2 = json.loads(run("install-hermes", "--rollback", str(backup), "--json", home=home, check=0).stdout)
    assert r2["ok"] and r2["replaced_kept_at"] != r["replaced_kept_at"]            # twice within a second: no crash


def test_install_profile_flag_targets_only_that_profile(home):
    (home / ".hermes" / "profiles" / "friend").mkdir(parents=True)
    d = json.loads(run("install-hermes", "--profile", "friend", "--json", home=home, check=0).stdout)
    assert d["ok"] and d["hermes_home"].endswith(os.path.join("profiles", "friend"))
    assert (home / ".hermes" / "profiles" / "friend" / "plugins" / "hermind" / "store.py").exists()
    assert not (home / ".hermes" / "plugins").exists()


def test_install_refuses_a_missing_hermes_home(home, tmp_path):
    r = run("install-hermes", "--hermes-home", str(tmp_path / "nowhere"), "--json", home=home)
    d = json.loads(r.stdout)
    assert r.returncode != 0 and d["ok"] is False and "does not exist" in d["error"]


def _bare_home(tmp_path):
    h = tmp_path / "bare"                 # no ~/.hermes at all: Hermes is not installed
    h.mkdir()
    return h


def test_install_dry_run_without_hermes_says_skipped_not_failed_json(tmp_path):
    r = run("install-hermes", "--dry-run", "--json", home=_bare_home(tmp_path), check=0)
    d = json.loads(r.stdout)
    assert d["ok"] is True and d["skipped"] is True
    assert "not found" in d["reason"] and "MCP" in d["reason"] and "README.md" in d["reason"]
    assert "error" not in d


def test_install_dry_run_without_hermes_says_skipped_not_failed_text(tmp_path):
    r = run("install-hermes", "--dry-run", home=_bare_home(tmp_path), check=0)
    assert r.stdout.startswith("SKIPPED")
    assert "FAILED" not in r.stdout


def test_install_dry_run_with_an_explicit_missing_target_still_fails(home, tmp_path):
    # a --hermes-home or --profile that does not exist is more likely a typo than "no Hermes": keep the 1.0.1 error
    r = run("install-hermes", "--hermes-home", str(tmp_path / "nowhere"), "--dry-run", "--json", home=home)
    assert r.returncode == 2 and "does not exist" in json.loads(r.stdout)["error"]
    r = run("install-hermes", "--profile", "wrok", "--dry-run", home=home)
    assert r.returncode == 2 and r.stdout.startswith("FAILED")


def test_install_dry_run_with_hermes_present_is_unaffected(home):
    r = run("install-hermes", "--dry-run", home=home, check=0)
    assert r.stdout.startswith("OK")


def test_rollback_without_a_backup_is_an_error(home):
    r = run("install-hermes", "--rollback", "--json", home=home)
    assert r.returncode != 0 and "no plugin backup" in json.loads(r.stdout)["error"]


def test_config_list_and_unknown_client(home):
    d = json.loads(run("config", "--list", "--json", home=home, check=0).stdout)
    assert {"claude-code", "cursor", "codex", "vscode", "zed", "opencode", "dsh", "workbuddy"} <= set(d["clients"])
    r = run("config", "nope", "--json", home=home)
    assert r.returncode == 2 and "unknown client" in json.loads(r.stdout)["error"]


JSON_CLIENTS = ["cursor", "claude-desktop", "windsurf", "kiro", "cline", "gemini-cli", "antigravity-cli", "qwen-code", "workbuddy", "codebuddy",
                "trae", "warp", "cherry-studio", "vscode", "zed", "opencode", "amp", "openclaw"]


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_config_json_snippets_parse_and_carry_env(client, home):
    d = json.loads(run("config", client, "--json", "--write", "--db", "/data/m.db", "--key-file", "/k/db.key", home=home, check=0).stdout)
    obj = json.loads(d["snippet"])
    text = json.dumps(obj)
    assert ("hermind-mcp" in text and "HERMIND_MCP_WRITE" in text
            and _in_json(_native("/data/m.db")) in text and _in_json(_native("/k/db.key")) in text)
    assert d["tested"] is False


def test_config_toml_yaml_and_shell(home):
    tomllib = pytest.importorskip("tomllib")                   # Python 3.11+
    d = json.loads(run("config", "codex", "--json", "--write", home=home, check=0).stdout)
    t = tomllib.loads(d["snippet"])["mcp_servers"]["hermind"]
    assert t["command"] == "uvx" and t["env"]["HERMIND_MCP_WRITE"] == "1"
    d = json.loads(run("config", "claude-code", "--json", "--source", "pipx", home=home, check=0).stdout)
    assert d["snippet"] == "claude mcp add hermind -- hermind-mcp" and d["tested"] is True
    d = json.loads(run("config", "continue", "--json", home=home, check=0).stdout)
    assert "mcpServers:" in d["snippet"] and "hermind-mcp" in d["snippet"]
    d = json.loads(run("config", "dsh", "--json", "--source", "checkout", home=home, check=0).stdout)
    assert _in_json(str(ROOT / "hermind_mcp.py")) in d["snippet"] and "checkout" in d["note"]


def test_config_checkout_source_runs_the_server(home, tmp_path):
    """The command the snippet gives for --source checkout really starts a working server."""
    d = json.loads(run("config", "cursor", "--json", "--source", "checkout", "--db", str(tmp_path / "x.db"), home=home, check=0).stdout)
    srv = json.loads(d["snippet"])["mcpServers"]["hermind"]
    env = {**os.environ, **srv["env"]}
    p = subprocess.run([srv["command"], *srv["args"]], input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
                       capture_output=True, text=True, env=env, timeout=60)
    assert "memory_search" in p.stdout


def _path_without_hermes(tmp):
    """A PATH with no `hermes` command. The interpreter's own folder is left out: in a venv that also holds Hermes it
    would bring `hermes` along. run() starts the CLI by the interpreter's absolute path, so nothing else is needed."""
    path = os.pathsep.join(p for p in ("/usr/bin", "/bin") if os.path.isdir(p)) or str(tmp)
    if shutil.which("hermes", path=path):
        pytest.skip("a `hermes` command is installed system-wide")
    return {"PATH": path}


def test_questions_follow_the_machine(home):
    bare = _path_without_hermes(home)                                                  # no `hermes` command on PATH
    d = json.loads(run("questions", "--json", home=home, extra_env=bare, check=0).stdout)
    ids = [q["id"] for q in d["questions"]]
    assert ids[0] == "target" and "write" in ids and "client_cli" in ids and "client_app" in ids         # no Hermes here: no profile question
    assert d["questions"][0]["choices"][0]["value"] == "mcp-cli"                       # nothing to upgrade, so MCP is offered first
    _seed_db(home / ".hermes" / "decision_memory.db")
    (home / ".hermes" / "profiles" / "friend" / "plugins" / "hermind").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "friend" / "plugins" / "hermind" / "plugin.yaml").write_text("version: 1.2.0\n")
    d = json.loads(run("questions", "--json", home=home, extra_env=bare, check=0).stdout)
    q = {x["id"]: x for x in d["questions"]}
    assert d["questions"][0]["choices"][0]["value"] == "hermes"
    assert [c["value"] for c in q["profiles"]["choices"]] == ["default", "friend"] and q["profiles"]["multi_select"]
    assert "database" in q and all(len(x["choices"]) <= 4 for x in d["questions"])  # fits a 4-choice question form
    assert q["write"]["choices"][0]["value"] == "read-only"


def test_config_groups_and_db_from_profile(home):
    d = json.loads(run("config", "--list", "--json", home=home, check=0).stdout)
    g = d["groups_by_how_you_apply_the_config"]
    assert sorted(sum(g.values(), [])) == d["clients"]
    d = json.loads(run("config", "cursor", "--db-from-profile", "friend", "--json", home=home, check=0).stdout)
    assert d["apply"] == "file" and _in_json(os.path.join("profiles", "friend", "hermind.db")) in d["snippet"]
    assert json.loads(run("config", "claude-code", "--json", home=home, check=0).stdout)["apply"] == "command"


def test_install_skill_dry_run_install_update(home):
    r = json.loads(run("install-skill", "--dry-run", "--json", home=home, check=0).stdout)
    assert {x["target"] for x in r["results"]} == {"hermes"} and not (home / ".claude").exists()   # only Hermes is set up
    r = json.loads(run("install-skill", "--target", "both", "--dry-run", "--json", home=home, check=0).stdout)
    assert {x["target"] for x in r["results"]} == {"hermes"}
    run("install-skill", "--target", "claude-code", home=home, check=0)
    f = home / ".claude" / "skills" / "hermind-setup" / "SKILL.md"
    assert f.read_text().startswith("---\nname: hermind-setup")
    assert "already current" in run("install-skill", "--target", "claude-code", "--dry-run", home=home).stdout
    f.write_text("old")
    run("install-skill", "--target", "claude-code", home=home, check=0)
    assert f.read_text().startswith("---") and (home / ".claude" / "skills" / "hermind-setup.bak" / "SKILL.md").read_text() == "old"


def test_skill_frontmatter_is_valid():
    head = (REPO / "skills" / "hermind-setup" / "SKILL.md").read_text().split("---")[1]
    kv = dict(l.split(": ", 1) for l in head.strip().splitlines())
    assert kv["name"] == "hermind-setup" and kv["description"].startswith("Use when") and len(kv["description"]) <= 60


def test_questions_split_hermes_cli_and_apps(home):
    bare = _path_without_hermes(home)
    d = json.loads(run("questions", "--json", home=home, extra_env=bare, check=0).stdout)
    q = {x["id"]: x for x in d["questions"]}
    assert [c["value"] for c in q["target"]["choices"]] == ["mcp-cli", "mcp-app", "hermes", "several"]
    assert q["hermes_where"]["when"]["target"] == ["hermes", "several"]
    assert {c["value"] for c in q["hermes_where"]["choices"]} == {"here", "remote"}
    assert q["client_cli"]["when"]["target"] == ["mcp-cli", "several"] and q["client_app"]["when"]["target"] == ["mcp-app", "several"]
    cli = {c["value"] for c in q["client_cli"]["choices"]}
    app = {c["value"] for c in q["client_app"]["choices"]}
    import importlib.util
    spec = importlib.util.spec_from_file_location("hermind_cli_for_surfaces", CLI)
    cli_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_mod)
    assert cli <= set(cli_mod.SURFACE["cli"]) and "chatgpt-desktop" in app and not (cli & app)
    assert all(len(x["choices"]) <= 4 for x in d["questions"])


def test_surfaces_cover_every_client_once_and_codex_aliases_share_one_entry(home):
    d = json.loads(run("config", "--list", "--json", home=home, check=0).stdout)
    flat = sum(d["groups_by_what_you_run"].values(), [])
    assert sorted(flat) == d["clients"]
    a = json.loads(run("config", "chatgpt-desktop", "--json", home=home, check=0).stdout)
    b = json.loads(run("config", "codex", "--json", home=home, check=0).stdout)
    assert a["snippet"] == b["snippet"] and a["surface"] == "desktop" and b["surface"] == "cli" and "same file" in a["note_alias"]
    assert json.loads(run("config", "codex-ide", "--json", home=home, check=0).stdout)["surface"] == "ide"


FAKE_SSH = """#!/bin/sh
# stands in for ssh: skip the options, drop the host, run the remote command string locally (same quoting rules as a remote login shell)
while [ $# -gt 0 ]; do
  case "$1" in -T) shift;; -o|-p|-i) shift 2;; *) break;; esac
done
shift
exec sh -c "$1"
"""


def _remote(home, tmp_path, *extra):
    d = json.loads(run("config", "cursor", "--json", "--remote", "me@box", "--remote-checkout", str(REPO), *extra, home=home, check=0).stdout)
    return d, json.loads(d["snippet"])["mcpServers"]["hermind"]


def test_remote_config_runs_the_server_through_ssh(home, tmp_path):
    _seed_db(home / ".hermes" / "profiles" / "friend" / "decision_memory.db") if (home / ".hermes" / "profiles" / "friend").mkdir(parents=True) is None else None
    d, srv = _remote(home, tmp_path, "--remote-hermes-profile", "friend", "--ssh-port", "2222", "--ssh-key", "/k/id")
    assert srv["command"] == "ssh" and "env" not in srv                                  # nothing secret or local in the client config
    assert srv["args"][:4] == ["-T", "-o", "BatchMode=yes", "-o"] and ["-p", "2222"] == srv["args"][srv["args"].index("-p"):][:2]
    assert srv["args"][-2] == "me@box" and 'HERMIND_DB="$HOME/.hermes/profiles/friend/hermind.db"' in srv["args"][-1]
    assert "over ssh" in d["note"]
    if sys.platform == "win32":      # the stand-in ssh below is a /bin/sh script: Windows cannot run it by its shebang (it would find the real ssh.exe)
        return
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text(FAKE_SSH)
    (fake / "ssh").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USERPROFILE": str(home)}
    msgs = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"deploy window"}}}\n'
    p = subprocess.run([srv["command"], *srv["args"]], input=msgs, capture_output=True, text=True, env=env, timeout=60)
    assert "Tuesday" in p.stdout, (p.stdout, p.stderr)                                   # read the friend profile's db "on the remote"


def test_remote_write_flag_and_odd_paths(home, tmp_path):
    d, srv = _remote(home, tmp_path, "--write", "--db", "/data/my notes.db", "--key-file", "~/keys/a$b.key")
    cmd = srv["args"][-1]
    assert "HERMIND_MCP_WRITE=1" in cmd and "'/data/my notes.db'" in cmd and '"$HOME/keys/a\\$b.key"' in cmd


def test_remote_conflicting_options_are_refused(home):
    r = run("config", "cursor", "--remote", "me@box", "--db-from-profile", "friend", "--json", home=home)
    assert r.returncode == 2 and "--remote" in json.loads(r.stdout)["error"]
    r = run("config", "cursor", "--remote-hermes-profile", "friend", "--json", home=home)
    assert r.returncode == 2
    d = json.loads(run("questions", "--json", home=home, check=0).stdout)
    # the cross-machine choice exists whenever the database question is asked
    for q in d["questions"]:
        if q["id"] == "database":
            assert "shared-remote" in [c["value"] for c in q["choices"]] and len(q["choices"]) <= 4


def _keyline(home, *args):
    r = run("authorize-key", "--json", *args, home=home)
    return r, json.loads(r.stdout)


@pytest.mark.xfail(sys.platform == "win32", strict=True, reason=(
    "Windows: authorize-key writes a POSIX command line (python3, single quotes around the backslash paths), which a "
    "Windows OpenSSH server (cmd.exe) can neither parse nor run"))
def test_authorize_key_line_and_errors(home, tmp_path):
    _seed_db(home / ".hermes" / "decision_memory.db")
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc test@mac\n")
    r, d = _keyline(home, "--pubkey", str(pub))
    assert r.returncode == 0 and d["write"] is False
    line = d["authorized_keys_line"]
    assert line.startswith('restrict,command="python3 ') and "hermind_mcp.py --db " in line and "--write" not in line
    assert line.endswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc test@mac")
    assert "--write" in _keyline(home, "--pubkey", str(pub), "--write")[1]["authorized_keys_line"]
    assert _keyline(home, "--pubkey", str(tmp_path / "nope"))[0].returncode == 2
    assert "does not exist" in _keyline(home, "--pubkey", str(pub), "--db", str(tmp_path / "missing.db"))[1]["error"]
    assert "does not exist" in _keyline(home, "--pubkey", str(pub), "--profile", "ghost")[1]["error"]
    bad = tmp_path / "bad.pub"
    bad.write_text("not a key\n")
    assert "need one public key" in _keyline(home, "--pubkey", str(bad))[1]["error"]
    odd = tmp_path / 'we"ird.db'                                   # a double quote in the path would break out of command="..."
    odd.write_bytes((home / ".hermes" / "decision_memory.db").read_bytes())
    r, d = _keyline(home, "--pubkey", str(pub), "--db", str(odd))
    assert r.returncode == 2 and "double quote" in d["error"]
    spaced = tmp_path / "my notes.db"
    spaced.write_bytes(odd.read_bytes())
    assert "'" + str(spaced) + "'" in _keyline(home, "--pubkey", str(pub), "--db", str(spaced))[1]["authorized_keys_line"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply POSIX file modes")
def test_authorize_key_generate_never_overwrites(home, tmp_path):
    if not __import__("shutil").which("ssh-keygen"):
        pytest.skip("no ssh-keygen")
    _seed_db(home / ".hermes" / "decision_memory.db")
    key = tmp_path / "id_hermind"
    r, d = _keyline(home, "--generate", str(key))
    assert r.returncode == 0 and key.exists() and (key.stat().st_mode & 0o077) == 0 and "private_key" in d
    key.write_text("precious")
    r, d = _keyline(home, "--generate", str(key))
    assert r.returncode == 2 and "refusing" in d["error"] and key.read_text() == "precious"


def test_config_restricted_sends_no_command(home):
    d = json.loads(run("config", "cursor", "--remote", "me@box", "--restricted", "--ssh-key", "/k/restricted", "--json", home=home, check=0).stdout)
    args = json.loads(d["snippet"])["mcpServers"]["hermind"]["args"]
    assert args[-1] == "me@box" and args[args.index("-i") + 1] == _native("/k/restricted") and "forced command" in d["note"]
    assert run("config", "cursor", "--remote", "me@box", "--restricted", "--ssh-key", "/k/r", "--write", "--json", home=home).returncode == 2
    assert run("config", "cursor", "--restricted", "--ssh-key", "/k/r", "--json", home=home).returncode == 2


def test_config_restricted_without_ssh_key_is_refused_not_marked_tested(home):
    for client in ("claude-code", "cursor"):
        r = run("config", client, "--remote", "me@box", "--restricted", "--json", home=home)
        d = json.loads(r.stdout)
        assert r.returncode == 2 and "--ssh-key" in d["error"] and "snippet" not in d and "tested" not in d


@pytest.mark.skipif(not os.path.exists("/usr/sbin/sshd") or not __import__("shutil").which("ssh-keygen"), reason="needs sshd and ssh-keygen")
def test_restricted_key_against_a_real_sshd(home, tmp_path):
    """A real sshd on localhost: the key can read notes, and nothing else."""
    import socket, time
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    for n in ("hostkey", "client"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / n)], check=True)
    db = tmp_path / "notes.db"
    _seed_db(db)
    r, d = _keyline(home, "--pubkey", str(tmp_path / "client.pub"), "--db", str(db), "--python", sys.executable)
    assert r.returncode == 0, d
    (tmp_path / "authorized_keys").write_text(d["authorized_keys_line"] + "\n")
    (tmp_path / "authorized_keys").chmod(0o600)
    (tmp_path / "sshd_config").write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp_path}/hostkey\nPidFile {tmp_path}/pid\n"
                                          f"AuthorizedKeysFile {tmp_path}/authorized_keys\nPasswordAuthentication no\nUsePAM no\nStrictModes no\n")
    subprocess.run(["/usr/sbin/sshd", "-f", str(tmp_path / "sshd_config"), "-E", str(tmp_path / "log")], check=True)
    try:
        time.sleep(1)
        known = tmp_path / "kh"
        known.write_text("[127.0.0.1]:%d %s" % (port, (tmp_path / "hostkey.pub").read_text()))
        base = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=yes",
                "-o", "IdentitiesOnly=yes", "-i", str(tmp_path / "client"), "-p", str(port)]
        user = os.environ.get("USER") or __import__("getpass").getuser()
        host = f"{user}@127.0.0.1"
        call = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s}}\n'
        # 1. reading works, and the client's own command ("echo pwned") is ignored
        p = subprocess.run(base + [host, "echo pwned"], input=call % ("memory_search", '{"query":"deploy window"}'), capture_output=True, text=True, timeout=60)
        assert "Tuesday" in p.stdout and "pwned" not in p.stdout, (p.stdout, p.stderr)
        # 2. writing is refused (no --write in the forced command), and the note count did not change
        p = subprocess.run(base + [host], input=call % ("memory_remember", '{"content":"x","keywords":"y"}'), capture_output=True, text=True, timeout=60)
        assert "error" in p.stdout.lower() and "Unknown tool" in p.stdout, p.stdout
        # 3. no port forwarding, no pty
        p = subprocess.run(base + ["-N", "-o", "ExitOnForwardFailure=yes", "-R", f"{port + 7}:127.0.0.1:22", host], input="", capture_output=True, text=True, timeout=30)
        assert p.returncode != 0, "the server accepted a remote port forward"
        # 4. a restricted --restricted config from `hermind config` is exactly this ssh invocation
        cfg = json.loads(json.loads(run("config", "cursor", "--remote", host, "--restricted", "--ssh-port", str(port), "--ssh-key", str(tmp_path / "client"),
                                        "--json", home=home, check=0).stdout)["snippet"])["mcpServers"]["hermind"]
        p = subprocess.run([cfg["command"], *cfg["args"][:-1], "-o", f"UserKnownHostsFile={known}", cfg["args"][-1]],
                           input=call % ("memory_search", '{"query":"deploy window"}'), capture_output=True, text=True, timeout=60)
        assert "Tuesday" in p.stdout, (p.stdout, p.stderr)
    finally:
        pid = (tmp_path / "pid").read_text().strip() if (tmp_path / "pid").exists() else ""
        if pid:
            os.kill(int(pid), 15)



def test_doctor_explains_a_higher_numbered_pre_public_build(home):
    p = home / ".hermes" / "plugins" / "hermind"
    p.mkdir(parents=True)
    (p / "plugin.yaml").write_text("name: hermind\nversion: 1.4.0\n")
    d = json.loads(run("doctor", "--json", home=home, check=0).stdout)
    assert any("before the public 1.0.0 numbering" in s and "not a newer program" in s for s in d["next_steps"])


def test_doctor_suggests_semantic_search_when_it_is_not_set_up(home, monkeypatch):
    monkeypatch.setenv("HERMIND_VEC_HOME", str(home / "no-such-dir"))
    d = json.loads(run("doctor", "--json", home=home, check=0).stdout)
    assert d["semantic_search"]["set_up"] is False
    assert any("semantic search" in s and "hermind_cli.py setup-vectors balanced" in s for s in d["next_steps"])  # a checkout


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply POSIX file modes")
def test_remember_search_export_roundtrip(home, tmp_path):
    db = tmp_path / "m.db"
    r = run("remember", "Atlas uses PostgreSQL for orders", "--keywords", "orders database", "--db", str(db), home=home, check=0)
    assert r.stdout.startswith("saved note #1 (kind: ")
    r = run("remember", "我哋用 Redis 做 cache", "--db", str(db), home=home, check=0)
    assert r.stdout.startswith("saved note #2 (kind: ")
    s = run("search", "which database for orders", "--db", str(db), home=home, check=0)
    assert "PostgreSQL" in s.stdout.splitlines()[0]
    out = tmp_path / "e.json"
    r = run("export", "--db", str(db), "--out", str(out), home=home, check=0)
    assert r.stdout.strip() == f"exported 2 notes to {out} (active 2)"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 2


def test_remember_prints_a_readable_line_not_a_dict(home, tmp_path):
    db = tmp_path / "m.db"
    first = run("remember", "Atlas uses PostgreSQL for orders", "--db", str(db), home=home, check=0)
    again = run("remember", "Atlas uses PostgreSQL for orders", "--db", str(db), home=home, check=0)
    assert first.stdout.strip() == "saved note #1 (kind: chat, does not expire)"
    assert again.stdout.strip() == "already saved as note #1 (kind: chat, does not expire)"
    assert "{" not in first.stdout + again.stdout
    j = run("remember", "Atlas uses PostgreSQL for orders", "--db", str(db), "--json", home=home, check=0)
    assert json.loads(j.stdout) == {"ok": True, "database": str(db), "id": 1, "created": False, "kind": "chat",
                                 "expires": None}


def test_search_missing_db_is_an_error_not_a_traceback(home, tmp_path):
    r = run("search", "x", "--db", str(tmp_path / "none.db"), home=home, check=2)
    assert "does not exist" in r.stdout + r.stderr and "Traceback" not in r.stderr


def test_remember_refuses_a_secret_with_a_clean_error(home, tmp_path):
    r = run("remember", "key sk-" + "a" * 40, "--db", str(tmp_path / "m.db"), home=home, check=2)
    assert "refused" in r.stdout + r.stderr and "Traceback" not in r.stderr
    r = run("remember", "", "--db", str(tmp_path / "m.db"), home=home)
    assert r.returncode == 2 and "Traceback" not in r.stderr


def test_doctor_share_has_no_home_path_user_or_profile_name(home):
    prof = home / ".hermes" / "profiles" / "alice-secret-profile"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("memory:\n  provider: hermind\n")
    raw = run("doctor", "--json", home=home, check=0).stdout
    r = run("doctor", "--share", home=home, check=0)
    d = json.loads(r.stdout)
    assert str(home) not in r.stdout and "alice-secret-profile" not in r.stdout
    assert _in_json(str(home)) not in r.stdout              # JSON doubles a Windows path's backslashes: look for that spelling too
    assert "/bin/" not in json.dumps(d.get("python")) and all(isinstance(v, bool) for v in d["commands"].values())
    assert d["hermind"]["version"] and "shared_safely" in d
    assert _in_json(str(home)) in raw   # the plain report does carry the path, which is why --share exists


def test_hook_source_follows_how_hermind_is_installed(monkeypatch, tmp_path):
    """A checkout points at itself; an installed `hermind-hook` on PATH is used as is; a `uvx --from` run (whose command
    exists only inside uvx's own environment) gets the uvx command from the release tag, so the hook still runs later."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hermind_cli_src", CLI)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.default_source() == "checkout"
    monkeypatch.setattr(m, "ROOT", tmp_path)                                         # not a checkout
    exe = "hermind-hook.exe" if os.name == "nt" else "hermind-hook"                   # a Windows command needs an extension
    own = tmp_path / "uvx-env" / ("Scripts" if os.name == "nt" else "bin")           # where an environment keeps its commands
    own.mkdir(parents=True)
    (own / exe).write_text("#!/bin/sh\n")
    (own / exe).chmod(0o755)
    monkeypatch.setattr(m.sys, "prefix", str(own.parent))
    monkeypatch.setenv("PATH", f"{own}{os.pathsep}/usr/bin{os.pathsep}/bin")
    if not shutil.which("hermind-hook", path=f"/usr/bin{os.pathsep}/bin"):
        assert m.default_source() == "git"                                           # only inside uvx's env
    elsewhere = tmp_path / "local-bin"
    elsewhere.mkdir()
    (elsewhere / exe).write_text("#!/bin/sh\n")
    (elsewhere / exe).chmod(0o755)
    monkeypatch.setenv("PATH", f"{own}{os.pathsep}{elsewhere}")
    assert m.default_source() == "pipx"
    assert str(own) not in m._path_without_own_bin().split(os.pathsep)


def test_install_skill_only_for_agents_that_are_set_up(tmp_path):
    h = tmp_path / "fresh"
    h.mkdir()
    r = run("install-skill", "--json", home=h)
    assert r.returncode != 0 and "pass --target" in json.loads(r.stdout)["error"] and not (h / ".hermes").exists()
    (h / ".claude").mkdir()
    d = json.loads(run("install-skill", "--json", home=h, check=0).stdout)
    assert [x["target"] for x in d["results"]] == ["claude-code"] and not (h / ".hermes").exists()   # no phantom Hermes


def test_unknown_client_says_so_and_hook_answers_help(home):
    r = run("config", "foo", home=home)
    assert r.returncode == 2 and r.stdout.startswith("ERROR: unknown client 'foo'")
    r = subprocess.run([sys.executable, str(ROOT / "hermind_hook.py"), "--help"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    assert r.returncode == 0 and "hermind-hook" in r.stdout


README_SENTENCE = "Atlas uses PostgreSQL for orders"


def _store(db):
    sys.path.insert(0, str(Path(CLI).parent))
    from store import DecisionStore
    return DecisionStore(str(db))


def _expiry_of(db, nid=1):
    s = _store(db)
    try:
        row = s.conn.execute("SELECT kind, expires_at FROM notes WHERE id=?", (nid,)).fetchone()
        return row["kind"], row["expires_at"]
    finally:
        s.close()


def _alive_after(db, days):
    s = _store(db)
    try:
        s.maintenance(now=time.time() + days * 86400, consolidate=False)
        return s.conn.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0]
    finally:
        s.close()


def test_remember_with_kind_auto_never_expires_but_keeps_the_classified_kind(home, tmp_path):
    db = tmp_path / "m.db"
    r = run("remember", README_SENTENCE, "--db", str(db), home=home, check=0)
    kind, expires = _expiry_of(db)
    assert kind == "chat" and expires is None                      # the text is classified chat; chat would expire in 90 days
    assert r.stdout.strip() == "saved note #1 (kind: chat, does not expire)"
    assert _alive_after(db, 400) == 1
    j = json.loads(run("remember", README_SENTENCE, "--db", str(db), "--json", home=home, check=0).stdout)
    assert j["kind"] == "chat" and j["expires"] is None


def test_remember_with_an_explicit_chat_or_news_kind_still_expires(home, tmp_path):
    db = tmp_path / "m.db"
    r = run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db), home=home, check=0)
    assert _expiry_of(db)[1] is not None and re.search(r"\(kind: chat, expires 20\d\d-\d\d-\d\d\)", r.stdout)
    assert _alive_after(db, 400) == 0
    db2 = tmp_path / "n.db"
    run("remember", "Zorblax maintenance window moves to Sunday", "--kind", "news", "--db", str(db2), home=home, check=0)
    assert _expiry_of(db2)[1] is not None
    assert _alive_after(db2, 20) == 0


def test_remembering_the_same_text_again_on_purpose_removes_an_earlier_expiry(home, tmp_path):
    db = tmp_path / "m.db"
    run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db), home=home, check=0)
    assert _expiry_of(db)[1] is not None
    r = run("remember", README_SENTENCE, "--db", str(db), home=home, check=0)
    assert r.stdout.startswith("already saved as note #1") and "does not expire" in r.stdout
    assert _expiry_of(db)[1] is None and _alive_after(db, 400) == 1


WEIRD_DB = "/tmp/my dir/it's m\\k.db"
WEIRD_KEY = '/tmp/key dir/"k" it\'s.key'
NATIVE_DB, NATIVE_KEY = _native(WEIRD_DB), _native(WEIRD_KEY)       # what the CLI makes of them (the same strings on Linux and macOS)


def _walk_find(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                return v
            found = _walk_find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk_find(v, key)
            if found is not None:
                return found
    return None


def _config(home, client, *more):
    return json.loads(run("config", client, "--json", "--write", "--db", WEIRD_DB, "--key-file", WEIRD_KEY, *more,
                          home=home, check=0).stdout)


@pytest.mark.parametrize("more", [(), ("--source", "checkout"), ("--remote", "me@host", "--remote-checkout", "/opt/it's dir/hermind")])
def test_config_claude_code_shell_line_quotes_every_value(home, more):
    import shlex
    line = _config(home, "claude-code", *more)["snippet"]
    argv = shlex.split(line)
    assert argv[:4] == ["claude", "mcp", "add", "hermind"]
    sep = argv.index("--")
    pairs = [argv[i + 1] for i, a in enumerate(argv[:sep]) if a == "-e"]
    reference = json.loads(_config(home, "cursor", *more)["snippet"])["mcpServers"]["hermind"]       # the same command, as JSON
    assert argv[sep + 1:] == [reference["command"], *reference["args"]]
    assert sorted(pairs) == sorted(f"{k}={v}" for k, v in reference.get("env", {}).items())
    if not more or more == ("--source", "checkout"):
        assert f"HERMIND_DB={NATIVE_DB}" in pairs and f"HERMIND_DB_KEY_FILE={NATIVE_KEY}" in pairs


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_config_json_snippets_keep_awkward_paths_intact(client, home):
    obj = json.loads(_config(home, client)["snippet"])
    env = _walk_find(obj, "env") or _walk_find(obj, "environment")
    assert env["HERMIND_DB"] == NATIVE_DB and env["HERMIND_DB_KEY_FILE"] == NATIVE_KEY


def test_config_toml_and_yaml_snippets_keep_awkward_paths_intact(home):
    tomllib = pytest.importorskip("tomllib")
    t = tomllib.loads(_config(home, "codex", "--source", "checkout")["snippet"])["mcp_servers"]["hermind"]
    assert t["env"]["HERMIND_DB"] == NATIVE_DB and t["env"]["HERMIND_DB_KEY_FILE"] == NATIVE_KEY
    assert t["command"] == sys.executable and t["args"] == [str(ROOT / "hermind_mcp.py")]
    for client in ("continue", "dsh"):                     # YAML: every value is a JSON string, which YAML reads the same way
        lines = _config(home, client, "--source", "checkout")["snippet"].splitlines()
        values = {ln.split(":", 1)[0].strip(): ln.split(":", 1)[1].strip() for ln in lines if ":" in ln}
        assert json.loads(values["command"]) == sys.executable
        assert json.loads(values["HERMIND_DB"]) == NATIVE_DB and json.loads(values["HERMIND_DB_KEY_FILE"]) == NATIVE_KEY


def test_install_hermes_outside_a_checkout_advises_the_pinned_clone(home, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT))
    import hermind_cli
    monkeypatch.setattr(hermind_cli, "ROOT", tmp_path / "not-a-checkout")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    code = hermind_cli.main(["install-hermes", "--dry-run", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert code != 0 and d["ok"] is False
    assert f"clone --branch v{hermind_cli.version()} https://github.com/fungane/hermind.git" in d["error"]
    assert "clone https://github.com/fungane/hermind " not in d["error"]


def test_config_restricted_ssh_uses_only_the_named_key(home):
    """-i alone still offers the agent's keys first: a normal authorised key would open a shell. IdentitiesOnly=yes stops that."""
    for client in ("cursor", "zed", "codex", "continue", "opencode"):
        d = json.loads(run("config", client, "--remote", "me@box", "--restricted", "--ssh-key", "/k/r", "--json", home=home).stdout)
        assert "IdentitiesOnly=yes" in d["snippet"] and d["snippet"].index("IdentitiesOnly=yes") < d["snippet"].index(_in_json(_native("/k/r"))), client
    d = json.loads(run("config", "cursor", "--remote", "me@box", "--ssh-key", "/k/r", "--json", home=home).stdout)   # unrestricted too
    assert "IdentitiesOnly=yes" in d["snippet"]
    d = json.loads(run("config", "cursor", "--remote", "me@box", "--json", home=home).stdout)
    assert "IdentitiesOnly" not in d["snippet"] and "-i" not in json.loads(d["snippet"])["mcpServers"]["hermind"]["args"]
    d = run("config", "claude-code", "--remote", "me@box", "--restricted", "--ssh-key", "/k/r", home=home).stdout
    assert "IdentitiesOnly=yes" in d


def test_config_errors_in_text_mode_are_one_error_line(home):
    for argv in (["claude-code", "--remote", "a@b", "--restricted"], ["cursor", "--restricted", "--ssh-key", "/k"],
                 ["cursor", "--trust-writes"], ["cursor", "--remote-hermes-profile", "x"]):
        r = run("config", *argv, home=home)
        assert r.returncode == 2 and r.stdout.startswith("ERROR: ") and "Traceback" not in r.stderr and "KeyError" not in r.stderr, (argv, r)
        assert r.stdout.count("\n") == 1


def test_remembering_again_with_an_explicit_never_expiring_kind_removes_an_earlier_expiry(home, tmp_path):
    """Round 2: --kind decision kept the old chat copy's 90 days; on an already expired copy the next purge removed it."""
    db = tmp_path / "k.db"
    run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db), home=home, check=0)
    assert _expiry_of(db)[1] is not None
    r = run("remember", README_SENTENCE, "--kind", "decision", "--db", str(db), home=home, check=0)
    assert r.stdout.startswith("already saved as note #1") and "does not expire" in r.stdout and "expires 20" not in r.stdout
    assert _expiry_of(db) == ("chat", None) and _alive_after(db, 400) == 1
    # an already expired copy is rescued too
    db2 = tmp_path / "k2.db"
    run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db2), home=home, check=0)
    s = _store(db2)
    with s.conn:
        s.conn.execute("UPDATE notes SET expires_at=?", (time.time() - 86400,))
    s.close()
    r = run("remember", README_SENTENCE, "--kind", "decision", "--db", str(db2), home=home, check=0)
    assert "does not expire" in r.stdout and _alive_after(db2, 400) == 1
    # a re-save that would itself expire (explicit chat) leaves the earlier expiry alone
    db3 = tmp_path / "k3.db"
    run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db3), home=home, check=0)
    before = _expiry_of(db3)
    run("remember", README_SENTENCE, "--kind", "chat", "--db", str(db3), home=home, check=0)
    assert _expiry_of(db3) == before and before[1] is not None


def test_shell_quote_is_shlex_on_posix_and_double_quotes_on_windows():
    """Windows 3.10/3.14 CI: the hook line was quoted with single quotes, which cmd.exe does not know, so Claude Code's hook never ran."""
    import shlex
    sys.path.insert(0, str(ROOT))
    import hermind_cli
    q = hermind_cli.shell_quote
    values = ["plain", "hermind-hook", "git+https://github.com/fungane/hermind@v1.0.0", "a b", "it's", 'say "hi"', "", "$HOME/x",
              WEIRD_DB, WEIRD_KEY, r"C:\Users\me\my notes\m.db"]
    assert all(q(v, windows=False) == shlex.quote(v) for v in values)             # POSIX: exactly what it always was
    assert q("hermind-hook", windows=True) == "hermind-hook" and q("git+https://github.com/fungane/hermind@v1.0.0", windows=True).isascii()
    assert q(r"C:\Program Files\Python 3\python.exe", windows=True) == r'"C:\Program Files\Python 3\python.exe"'
    assert q(r"C:\Python3\python.exe", windows=True) == r'"C:\Python3\python.exe"'      # a bare backslash path is an escape sequence in bash
    assert q("C:\\dir\\", windows=True) == '"C:\\dir\\\\"' and q("", windows=True) == '""'
    for v in values:
        assert shlex.split(q(v, windows=True)) == [v]                                    # and the value comes back whole


def test_hook_command_on_windows_runs_in_cmd_exe_and_in_bash(monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT))
    import hermind_cli
    from pathlib import PureWindowsPath
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Python 3\python.exe")
    monkeypatch.setattr(hermind_cli, "ROOT", PureWindowsPath(r"D:\a\hermind\hermind\hermind"))
    db = r"C:\Users\me\my notes\m.db"
    assert hermind_cli.hook_command(SimpleNamespace(source="checkout"), db, windows=True) == (
        r'"C:\Program Files\Python 3\python.exe" "D:\a\hermind\hermind\hermind\hermind_hook.py" --db "C:\Users\me\my notes\m.db"')
    assert hermind_cli.hook_command(SimpleNamespace(source="pipx"), None, windows=True) == "hermind-hook"
    assert hermind_cli.hook_command(SimpleNamespace(source="git"), db, windows=True).startswith(
        'uvx --from git+https://github.com/fungane/hermind@v')
    import shlex                                                                          # POSIX: the line it always was
    assert hermind_cli.hook_command(SimpleNamespace(source="checkout"), "/tmp/m y.db", windows=False) == " ".join(
        shlex.quote(x) for x in [sys.executable, str(hermind_cli.ROOT / "hermind_hook.py"), "--db", "/tmp/m y.db"])
