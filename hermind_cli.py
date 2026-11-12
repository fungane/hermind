#!/usr/bin/env python3
"""hermind: check, install and wire up Hermind. Standard library only; every command can answer in JSON.

Built so that an AI agent can set Hermind up without guessing. Every command reports facts, changes nothing unless it is
install-hermes (and that one backs up first and has --dry-run and --rollback), and exits 0 on success, 1 on a failed
check, 2 on a usage or environment error.

    hermind doctor [--json]                      what is installed where, what is missing, what to do next
    hermind selftest [--json]                    start the MCP server on a throwaway database and talk to it
    hermind install-hermes [--profile NAME | --hermes-home DIR] [--dry-run] [--json]
    hermind install-hermes --rollback [BACKUP_DIR] [--profile NAME | --hermes-home DIR]
    hermind config --list | CLIENT [--db FILE | --db-from-profile NAME] [--write] [--source git|checkout|pipx] [--key-file FILE] [--json]
    hermind authorize-key (--pubkey FILE | --generate FILE) [--profile NAME | --db FILE] [--checkout DIR] [--write]   run on the machine that holds the notes
    hermind install-hook [--db FILE | --db-from-profile NAME] [--write | --remove]     automatic recall in Claude Code (preview first)
    hermind review [--profile NAME | --db FILE] [--confirm IDS|all] [--reject IDS]   notes an agent wrote wait here for you
    hermind audit-db [--profile NAME | --db FILE] [--fail-on-findings]               read-only scan for credentials and personal data
    hermind questions [--json]                   the questions to ask the user BEFORE installing, built from what is detected here
    hermind install-skill [--target hermes|claude-code|both] [--dry-run]   teach an agent to start from those questions

From a checkout: python3 hermind_cli.py ...   Installed with pip/uvx: hermind ...
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


IMPORT_HELP = """Bring notes you already have (Markdown or text files, Hermes' built-in memory, Claude Code, ChatGPT or Claude exports, instruction files, a pasted list) into Hermind.

Nothing is written until you say so:

  hermind import SOURCE             PREVIEW. Lists every entry found and what would happen to it (see the outcomes
                                    below). Changes nothing.
  hermind import SOURCE --apply     DO IT. First saves a backup copy of the database and prints where it is (the newest
                                    5 import backups are kept), then adds the new notes as one "batch". Notes you
                                    already have are never changed.
  hermind import --list             SHOW the batches imported so far and whether each one finished.
  hermind import --revert BATCH     PREVIEW taking a batch back. Add --apply to do it: the notes it created are
                                    removed, and so is what they taught the entity index; a note you edited, pinned or
                                    merged since then is kept and listed.
  hermind import --readers          LIST the kinds of source that can be imported.

Every entry gets exactly one outcome, and every outcome except "new" is listed with its reason:

  new         will be (or was) added as a note. If some of its details could not be used (a date that cannot be read,
              a kind Hermind does not know, a trust outside 0 to 1) they are left out and the line says which.
  version     an older version of a note that this import adds (from Hermind's own JSON export): it is kept as an
              older version of that new note, found by searches that include history, and never shown as a live note.
  duplicate   the same text is already in the database (or came earlier in this import): nothing is added.
  edited      the file is Hermind's own export of a note you have, and it differs from that note: nothing is applied
              (tools/import_md.py brings edits back into the note).
  history     not imported as a live note: a file that is an older version or a merged duplicate that Hermind's export
              kept for history (status: superseded / merged); an older version whose note is not new (a duplicate,
              refused or excluded: the reason says which); a note rejected in review; what ChatGPT shows you removed.
  refused     the store's own rules say no: credential-like text, text you asked to forget, text over the length limit,
              or nothing left outside <private>.
  unreadable  the file's text could not be decoded; the reason says how to make it readable (see --encoding).
  empty       no text.
  excluded    its origin matched an --exclude pattern.

Text and Markdown folders: .md, .markdown, .mdx and .txt files are notes (the index.md files that Hermind's exporter
writes are passed by); every other file is counted in one remark, never silently dropped. A file's title, name or file
name, and its description, are added in front of a body that does not already say them.

Readers (the preview names the one it chose; --from NAME picks one yourself, --readers lists them all):

  markdown        a folder of text notes, or one such file.
  hermes-builtin  Hermes' built-in memory files (MEMORY.md and USER.md).
  claude-code     Claude Code's configuration folder (usually ~/.claude: you give the path, nothing is guessed), one
                  project's memory folder, or a MEMORY.md index. It reads every project's memory, subagent memory and the
                  instruction files Claude Code loads (CLAUDE.md, CLAUDE.local.md, rules). Session transcripts and
                  credentials are never opened; other items are named in a note.
  chatgpt-export  the ZIP from ChatGPT's data export, or the folder it was unpacked into: the memory list, the memory
                  writes and the custom instructions. ChatGPT keeps old memory lists inside old conversations: anything
                  you removed or changed later is listed as "history" and left out unless you add --include-removed.
  claude-export   the ZIP from Claude's data export, or its folder: memories.json and each project's instructions and
                  description. Conversations and documents are not imported.
  hermind-json    Hermind's own JSON export (hermind export): live notes with their kind, keywords, tags, trust, pin, scope,
                  dates and review state; older versions become older versions of the new notes; links between the new
                  notes and entity aliases come too. Rejected notes are not imported. The first export format (no
                  status field) needs --from hermind-json.
  instructions    one instruction file of a coding agent (AGENTS.md, CLAUDE.md, GEMINI.md, .cursorrules ...): one note
                  per block under a heading. @path imports are listed, not followed.
  paste           a text file holding what an assistant answered when you asked it to list everything it remembers
                  about you. Only with --from paste. One note per list item, with its category.

The two export formats are not published; their readers follow third-party descriptions and were tested on made-up
exports only, so read the preview before you --apply.

When a source has several parts (every project of a Claude Code folder, the lists and writes of a ChatGPT export) the
preview ends with a "By source" section: one line per part, with its counts and the --exclude 'PATTERN' that leaves that
part out. Leaving a part out changes nothing else in the preview.

Imported notes are ordinary notes and never expire. A preference imported from another tool stays out of the core block
(the standing rules shown to the agent in every session) until it has been recalled, pinned or trusted, so an import cannot
push your own preferences out of it. Their idle clock starts at the import, so a note dated years ago is not ranked as
long forgotten. Add --db FILE to choose the database
(default: $HERMIND_DB or ~/.hermind/memory.db) and --json for machine-readable output."""


EXPORT_HELP = """Hand over everything in the database: every note in every status (active, merged, superseded, rejected), every field, the links between notes and the entities with their aliases. By default the output is a JSON array with one object per note; add --out FILE to write it to a file (mode 600, never over an existing file unless you add --force). With --format jsonl you get one line per note for tools that keep entities with observations (each line says in its text when the note is waiting for review, outdated, no longer valid or an older version), plus one line per link; older versions only with --include-history. With --format markdown --out DIR you get a folder with one Markdown file per note (front matter, index files; --by kind|entity, --scope NAME, --include-history, --force) that `hermind import` reads back.

Not exported: vectors, the search index, tombstones (digests of forgotten text) and the usage log. Copy the database file (tools/backup.py) to move those too.

Add --db FILE to choose the database (default: $HERMIND_DB or ~/.hermind/memory.db) and --json for a machine-readable result line."""


def default_db_file(home) -> Path:
    """hermind.db in a Hermes home, or the old decision_memory.db when that is what is there (see store_common)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from store_common import default_db_file as _f
    return _f(home)
GIT_REPO = "git+https://github.com/fungane/hermind"
PLAIN_MAGIC = b"SQLite format 3\x00"

RUNTIME_FILES = ["__init__.py", "llm.py", "pii.py", "dbdriver.py", "vec.py", "textnorm.py", "screen.py", "embed_server.py",
                 "import_hindsight.py", "db_backup.py", "vec_setup.py", "import_core.py", "import_readers.py", "md_notes.py", "text_blocks.py", "export_notes.py", "md_export.py", "reader_hermind_json.py", "reader_claude_code.py", "reader_exports.py", "synonyms.yaml", "plugin.yaml", "README.md"]   # plus store*.py; same list as tools/deploy.sh


def runtime_files(src: Path = ROOT) -> list[str]:
    return sorted(set(RUNTIME_FILES) | {p.name for p in src.glob("store*.py")})


def version(src: Path = ROOT) -> str:
    for name, rx in (("hermind_mcp.py", r'__version__\s*=\s*"([^"]+)"'), ("plugin.yaml", r"^version:\s*(\S+)")):
        try:
            m = re.search(rx, (src / name).read_text(encoding="utf-8"), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return "unknown"


def git_url() -> str:
    """The git source pinned to this release's tag, so a client installs the tested code and not whatever main is now."""
    v = version()
    return f"{GIT_REPO}@v{v}" if re.fullmatch(r"\d+\.\d+\.\d+", v) else GIT_REPO


def plugin_version(plugin_dir: Path) -> str | None:
    try:
        m = re.search(r"^version:\s*(\S+)", (plugin_dir / "plugin.yaml").read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def emit(data: dict, as_json: bool, text) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text(data))


# -- database facts ------------------------------------------------------------------------------------------------


def db_state(path: Path) -> dict:
    """{path, state: missing|empty|plain|encrypted, notes?, schema?, size}; reads nothing but counts."""
    out: dict = {"path": str(path)}
    try:
        size = path.stat().st_size
    except OSError:
        return {**out, "state": "missing"}
    out["size_kb"] = size // 1024
    if size == 0:
        return {**out, "state": "empty"}
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head != PLAIN_MAGIC:
        return {**out, "state": "encrypted"}
    out["state"] = "plain"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        out["schema"] = con.execute("PRAGMA user_version").fetchone()[0]
        out["notes"] = con.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)[:120]
    return out


# -- doctor --------------------------------------------------------------------------------------------------------

CLIENT_HINTS = {            # client -> (command on PATH, config files whose presence suggests it is installed)
    "claude-code": ("claude", ["~/.claude.json"]),
    "codex": ("codex", ["~/.codex/config.toml"]),
    "cursor": (None, ["~/.cursor"]),
    "gemini-cli": ("gemini", ["~/.gemini/settings.json"]),
    "antigravity-cli": ("agy", ["~/.gemini/antigravity-cli", "~/.gemini/config/mcp_config.json"]),
    "windsurf": (None, ["~/.codeium/windsurf", "~/.config/devin"]),
    "kiro": (None, ["~/.kiro"]),
    "workbuddy": (None, ["~/.workbuddy"]),
    "codebuddy": (None, ["~/.codebuddy"]),
    "opencode": ("opencode", ["~/.config/opencode"]),
    "zed": ("zed", ["~/.config/zed"]),
    "qwen-code": ("qwen", ["~/.qwen"]),
    "amp": ("amp", ["~/.config/amp"]),
    "goose": ("goose", ["~/.config/goose"]),
    "claude-desktop": (None, ["~/.config/Claude", "~/Library/Application Support/Claude"]),
    "chatgpt-desktop": (None, ["/Applications/ChatGPT.app", "~/Applications/ChatGPT.app"]),
    "dsh": ("dsh", ["~/.dsh"]),
}


def hermes_homes(home: Path) -> list[tuple[str, Path]]:
    base = Path(os.environ.get("HERMES_HOME", "")).expanduser() if os.environ.get("HERMES_HOME") else home / ".hermes"
    homes = [("default", base)]
    profiles = base / "profiles"
    if profiles.is_dir():
        homes += [(p.name, p) for p in sorted(profiles.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    return homes


def doctor() -> dict:
    home = Path.home()
    problems: list[str] = []
    steps: list[str] = []
    here = version()
    out: dict = {"hermind": {"version": here, "path": str(ROOT), "is_checkout": (ROOT / "tools" / "deploy.sh").exists()},
                 "python": {"version": sys.version.split()[0], "executable": sys.executable}}
    if sys.version_info < (3, 10):
        problems.append("Python 3.10 or newer is required")
    out["commands"] = {c: shutil.which(c) for c in ("uv", "uvx", "pipx", "git", "hermes", "claude")}
    try:
        import sqlcipher3  # noqa: F401
        has_cipher = True
    except ImportError:
        has_cipher = False
    out["encryption"] = {"sqlcipher3_importable": has_cipher, "key_file_set": bool(os.environ.get("HERMIND_DB_KEY_FILE")),
                         "key_set": bool(os.environ.get("HERMIND_DB_KEY"))}
    profiles = []
    for name, h in hermes_homes(home):
        if not h.is_dir():
            continue
        plugin = h / "plugins" / "hermind"
        info = {"name": name, "home": str(h), "plugin_version": plugin_version(plugin), "db": db_state(default_db_file(h))}
        profiles.append(info)
        if info["plugin_version"] and info["plugin_version"] != here:
            def tup(v):
                return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
            older_label = ("is numbered higher than this checkout ({}) because it is a build from before the public 1.0.0 numbering; "
                           "it is not a newer program").format(here) if tup(info["plugin_version"]) > tup(here) else f"is older than this checkout ({here})"
            steps.append(f"Hermes profile '{name}' runs {info['plugin_version']}, which {older_label}: "
                         f"python3 hermind_cli.py install-hermes --profile {name} --dry-run")
        if info["db"]["state"] == "encrypted" and not (out["encryption"]["key_file_set"] or out["encryption"]["key_set"]):
            problems.append(f"profile '{name}': database is encrypted but no HERMIND_DB_KEY_FILE / HERMIND_DB_KEY is set")
        if info["db"]["state"] == "encrypted" and not has_cipher:
            problems.append(f"profile '{name}': database is encrypted but sqlcipher3 is not importable here")
    out["hermes_profiles"] = profiles
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from vec import default_vec_home
    vec_home = Path(os.path.expanduser(default_vec_home()))
    has_model = (vec_home / "model" / "model_quantized.onnx").exists() or any(  # default profile, or light / balanced / quality
        (vec_home / f"model-{p}" / "model.onnx").exists() for p in ("light", "balanced", "quality"))
    out["semantic_search"] = {"set_up": has_model and (vec_home / "venv" / "bin" / "python").exists(),
                              "home": str(vec_home)}
    mcp_db = Path(os.path.expanduser(os.environ.get("HERMIND_DB", "~/.hermind/memory.db")))
    out["mcp_default_db"] = db_state(mcp_db)
    detected = []
    for client, (cmd, paths) in CLIENT_HINTS.items():
        if (cmd and shutil.which(cmd)) or any(Path(os.path.expanduser(p)).exists() for p in paths):
            detected.append(client)
    out["clients_detected"] = detected
    if not out["hermind"]["is_checkout"]:
        steps.append("install-hermes needs a git checkout (tools/deploy.sh is missing here); git clone --branch v{} https://github.com/fungane/hermind.git".format(version()))
    if not (out["commands"]["uvx"] or out["commands"]["pipx"]):
        steps.append("for `config --source git` install uv (https://docs.astral.sh/uv/) or use `--source checkout`")
    if not out["semantic_search"]["set_up"]:
        steps.append("optional: semantic search (four model profiles: a 52 to 568 MB model plus its Python packages, 250 MB to 1 GB on disk in all, no API key). Without it, a question that shares no word with the note is often "
                     "missed, which is the main weakness in English (see \"How well does it recall\" in docs/how-it-works.md). Set it up with `{} setup-vectors balanced`".format(
                         "python3 hermind_cli.py" if out["hermind"]["is_checkout"] else "hermind"))
    me = "python3 hermind_cli.py" if out["hermind"]["is_checkout"] else "hermind"
    steps.append(f"{me} selftest   (proves the MCP server runs on this machine)")
    if detected:
        steps.append(f"wire a detected client: {me} config " + detected[0])
    out["problems"], out["next_steps"] = problems, steps
    out["ok"] = not problems
    return out


def doctor_text(d: dict) -> str:
    lines = [f"hermind {d['hermind']['version']} at {d['hermind']['path']}  (python {d['python']['version']})"]
    for p in d["hermes_profiles"]:
        db = p["db"]
        lines.append(f"  hermes profile {p['name']}: plugin {p['plugin_version'] or 'not installed'}, db {db['state']}"
                     + (f", {db['notes']} notes" if "notes" in db else ""))
    lines.append(f"  mcp default db: {d['mcp_default_db']['state']}   clients seen: {', '.join(d['clients_detected']) or 'none'}")
    lines += [f"PROBLEM: {p}" for p in d["problems"]] + [f"next: {s}" for s in d["next_steps"]]
    return "\n".join(lines)


# -- selftest ------------------------------------------------------------------------------------------------------


class _Server:
    def __init__(self, *args: str, env: dict):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "hermind_mcp.py"), *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.n = 0

    def call(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return {}
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed: " + self.proc.stderr.read()[:300])
        return json.loads(line)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def rename_db(ns) -> dict:
    """Rename a Hermes home's decision_memory.db to hermind.db (dry run unless --apply). Refuses while another program has
    the file open, checks integrity first, and leaves a copy `decision_memory.db.bak-rename-<time>-<n>` next to it."""
    home = resolve_home(ns)
    old, new = home / "decision_memory.db", home / "hermind.db"
    if new.exists():
        return {"ok": False, "error": f"{new} already exists; nothing to do (or move one of the two files away first)"}
    if not old.exists():
        return {"ok": False, "error": f"{old} does not exist; nothing to rename"}
    plan = {"from": str(old), "to": str(new), "apply": bool(ns.apply)}
    if not ns.apply:
        return {"ok": True, **plan, "next_step": "stop the agent that uses this database, then run again with --apply"}
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import dbdriver
    from db_backup import BackupError, backup_database
    try:
        con = dbdriver.connect(str(old), timeout=1, isolation_level=None)
        try:
            # A write lock alone misses a program that is connected but idle, or reading: WAL mode lets both in. Leaving
            # WAL does not: SQLite refuses it at once while any other connection has the file open. Hermind turns WAL
            # back on the next time it opens the database.
            mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise RuntimeError(f"the journal mode stayed {mode}")
            con.execute("BEGIN EXCLUSIVE")                       # a file already in rollback mode: no reader may be in
        except Exception:
            con.close()
            return {"ok": False, **plan, "error": "another program has the database open: stop the agent (gateway, MCP client) first"}
        try:
            con.execute("COMMIT")
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ic != "ok":
                return {"ok": False, **plan, "error": f"integrity_check says {ic!r}; not renaming a damaged file"}
        finally:
            con.close()
    except dbdriver.EncryptionError as e:
        return {"ok": False, **plan, "error": str(e)}
    try:
        made = backup_database(old, prefix="bak-rename", keep=10)
    except (BackupError, OSError, dbdriver.EncryptionError) as e:
        return {"ok": False, **plan, "error": f"could not back it up first, so nothing was renamed ({e})"}
    if made is None:
        return {"ok": False, **plan, "error": "could not back it up first, so nothing was renamed"}
    for suffix in ("", "-wal", "-shm", ".vec.log"):
        src = Path(str(old) + suffix)
        if src.exists():
            os.rename(src, Path(str(new) + suffix))
    return {"ok": True, **plan, "backup": str(made.path),
            "next_step": "restart the agent; if your config sets db_path to decision_memory.db explicitly, change it to hermind.db"}


def rename_db_text(d: dict) -> str:
    if not d["ok"]:
        return "ERROR: " + d["error"]
    if not d["apply"]:
        return f"would rename {d['from']} -> {d['to']}\n{d['next_step']}"
    return f"renamed {d['from']} -> {d['to']}\nbackup copy: {d['backup']}\n{d['next_step']}"


def selftest() -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})

    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMIND_")}
    tmp = tempfile.mkdtemp(prefix="hermind-selftest-")
    db = os.path.join(tmp, "selftest.db")
    try:
        w = _Server("--db", db, "--write", "--agent", "selftest", env=env)
        try:
            init = w.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1"}})
            check("initialize", "result" in init and "serverInfo" in init.get("result", {}))
            w.call("notifications/initialized", notify=True)
            r = w.call("tools/call", {"name": "memory_remember", "arguments": {
                "content": "Selftest note: the quokka maintenance window is every Tuesday.", "kind": "decision",
                "keywords": "quokka maintenance window tuesday"}})
            check("write (--write)", "result" in r and not r["result"].get("isError"), json.dumps(r)[:160])
        finally:
            w.close()
        s = _Server("--db", db, "--agent", "selftest", env=env)
        try:
            s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1"}})
            s.call("notifications/initialized", notify=True)
            names = [t["name"] for t in s.call("tools/list")["result"]["tools"]]
            check("read-only tool list", names == ["memory_search", "memory_get", "memory_list", "memory_related"], ",".join(names))
            r = s.call("tools/call", {"name": "memory_search", "arguments": {"query": "quokka maintenance"}})
            check("search finds the note", "quokka" in json.dumps(r), json.dumps(r)[:160])
            r = s.call("tools/call", {"name": "memory_remember", "arguments": {"content": "x", "keywords": "y"}})
            check("write refused without --write", "error" in r, json.dumps(r)[:160])
            meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
            r = s.call("server/discover", {"_meta": meta})
            check("2026-07-28 server/discover", r.get("result", {}).get("resultType") == "complete", json.dumps(r)[:160])
        finally:
            s.close()
    except Exception as exc:                                  # a missing interpreter feature, a crash, a timeout
        check("server ran", False, f"{type(exc).__name__}: {exc}"[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "version": version(), "checks": checks}


def selftest_text(d: dict) -> str:
    return "\n".join([f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']}" + (f"  {c.get('detail', '')}" if not c["ok"] else "")
                      for c in d["checks"]] + [f"selftest {'passed' if d['ok'] else 'FAILED'} (hermind {d['version']})"])


# -- install-hermes ------------------------------------------------------------------------------------------------


def resolve_home(ns) -> Path:
    if ns.hermes_home:
        return Path(ns.hermes_home).expanduser()
    base = Path(os.environ["HERMES_HOME"]).expanduser() if os.environ.get("HERMES_HOME") else Path.home() / ".hermes"
    if ns.profile in (None, "default"):
        return base
    return base / "profiles" / ns.profile


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_hermes(ns) -> dict:
    home = resolve_home(ns)
    plugin = home / "plugins" / "hermind"
    res: dict = {"ok": False, "hermes_home": str(home), "profile": ns.profile or "default", "dry_run": ns.dry_run,
                 "to_version": version(), "from_version": plugin_version(plugin), "steps": []}
    steps = res["steps"]
    if not (ROOT / "tools" / "deploy.sh").exists():
        res["error"] = "this is not a git checkout (tools/ is missing); clone --branch v{} https://github.com/fungane/hermind.git and run it from there".format(version())
        return res
    if not home.is_dir():
        res["error"] = f"{home} does not exist: is Hermes installed for this profile? (use --hermes-home to point elsewhere)"
        return res
    files = runtime_files()
    missing = [f for f in files if not (ROOT / f).exists()]
    if missing:
        res["error"] = "checkout is incomplete, missing: " + ", ".join(missing)
        return res
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = home / "backups"
    plugin_backup = backups / f"hermind-plugin-{res['from_version'] or 'none'}-{stamp}"
    db = default_db_file(home)
    state = db_state(db)
    res["db"] = state
    steps.append(f"back up {plugin} -> {plugin_backup}" if plugin.exists() else "no plugin installed yet: nothing to back up")
    steps.append(f"back up database ({state['state']}) with tools/backup.py -> {backups / 'db'}" if state["state"] in ("plain", "encrypted")
                 else f"database is {state['state']}: no database backup needed")
    steps.append(f"copy {len(files)} runtime files and tools/ into {plugin}")
    steps.append("verify: checksums equal to the source, syntax check, open a COPY of the database and search it")
    if ns.dry_run:
        res["ok"] = True
        res["next"] = ["re-run without --dry-run to apply"]
        return res
    if plugin.exists():
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copytree(plugin, plugin_backup)
        res["plugin_backup"] = str(plugin_backup)
    if state["state"] in ("plain", "encrypted"):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "backup.py"), "--dest", str(backups / "db"), str(db)],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
        if r.returncode != 0:
            res["error"] = "database backup failed, nothing was changed: " + (r.stdout + r.stderr).strip()[:300]
            return res
        res["db_backup"] = r.stdout.strip().splitlines()[-1][:300]
    (plugin / "tools").mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(ROOT / f, plugin / f)
    for p in sorted((ROOT / "tools").iterdir()):
        if p.suffix in (".py", ".sh"):
            shutil.copy2(p, plugin / "tools" / p.name)
    bad = [f for f in files if sha(ROOT / f) != sha(plugin / f)]
    for f in ("__init__.py", "store.py", "pii.py", "dbdriver.py"):
        try:
            ast.parse((plugin / f).read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(f"{f}: {exc}")
    verify = {"files_identical": not bad}
    if bad:
        verify["mismatch"] = bad
    if state["state"] in ("plain", "encrypted"):
        work = Path(tempfile.mkdtemp(prefix="hermind-verify-"))
        try:
            copy = work / "hermind.db"
            shutil.copy2(db, copy)
            code = ("import sys; sys.path.insert(0, sys.argv[1]); from store import DecisionStore; s = DecisionStore(sys.argv[2]); "
                    "n = s.conn.execute(\"SELECT count(*) FROM notes WHERE status='active'\").fetchone()[0]; "
                    "s.search('test', limit=3); print(n)")
            r = subprocess.run([sys.executable, "-c", code, str(plugin), str(copy)], capture_output=True, text=True, timeout=120)
            verify["opens_database_copy"] = r.returncode == 0
            if r.returncode == 0:
                verify["active_notes"] = int(r.stdout.strip().splitlines()[-1])
            else:
                verify["error"] = (r.stderr.strip().splitlines() or ["?"])[-1][:300]
        finally:
            shutil.rmtree(work, ignore_errors=True)
    res["verify"] = verify
    res["ok"] = verify["files_identical"] and verify.get("opens_database_copy", True)
    res["next"] = [
        "restart the Hermes gateway and the desktop app so the new code loads (this command does not restart anything)",
        "if this profile does not use Hermind yet: hermes config set memory.provider hermind",
        "after the restart, check the log for \"Memory provider 'hermind' activated\"",
    ]
    if res.get("plugin_backup"):
        res["rollback"] = f"python3 hermind_cli.py install-hermes --rollback {plugin_backup}" + (
            f" --profile {ns.profile}" if ns.profile not in (None, "default") else "") + (f" --hermes-home {ns.hermes_home}" if ns.hermes_home else "")
    if not res["ok"]:
        res["error"] = "verification failed; the previous plugin is in plugin_backup (use the rollback command)"
    return res


def rollback_hermes(ns) -> dict:
    home = resolve_home(ns)
    plugin = home / "plugins" / "hermind"
    res: dict = {"ok": False, "hermes_home": str(home)}
    choice = Path(ns.rollback).expanduser() if ns.rollback and ns.rollback is not True else None
    if choice is None:
        found = sorted((home / "backups").glob("hermind-plugin-*"))
        choice = found[-1] if found else None
    if choice is None or not (choice / "plugin.yaml").exists():
        res["error"] = "no plugin backup found (looked in " + str(home / "backups") + ")"
        return res
    aside = plugin.with_name(f"hermind.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while aside.exists():                         # two rollbacks within one second
        aside = plugin.with_name(f"hermind.replaced-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        n += 1
    if plugin.exists():
        plugin.rename(aside)
        res["replaced_kept_at"] = str(aside)
    shutil.copytree(choice, plugin)
    res.update(ok=True, restored_from=str(choice), restored_version=plugin_version(plugin),
               next=["restart the Hermes gateway and the desktop app"])
    return res


def install_text(d: dict) -> str:
    lines = [("OK" if d["ok"] else "FAILED") + f"  hermes home {d['hermes_home']}" + (" (dry run)" if d.get("dry_run") else "")]
    if "from_version" in d:
        lines.append(f"  {d['from_version'] or 'not installed'} -> {d['to_version']}")
    lines += [f"  step: {s}" for s in d.get("steps", [])]
    if "verify" in d:
        lines.append("  verify: " + json.dumps(d["verify"]))
    if d.get("error"):
        lines.append("  ERROR: " + d["error"])
    lines += [f"  next: {n}" for n in d.get("next", [])]
    if d.get("rollback"):
        lines.append("  rollback: " + d["rollback"])
    return "\n".join(lines)


# -- config snippets -----------------------------------------------------------------------------------------------

MCPSERVERS_JSON = {                 # client -> where its mcpServers JSON goes
    "cursor": "~/.cursor/mcp.json (or .cursor/mcp.json in a project)",
    "claude-desktop": "claude_desktop_config.json (Settings > Developer > Edit Config)",
    "windsurf": "~/.config/devin/mcp_config.json (older: ~/.codeium/windsurf/mcp_config.json)",
    "kiro": "~/.kiro/settings/mcp.json or .kiro/settings/mcp.json",
    "cline": "the cline_mcp_settings.json that Cline > MCP Servers > Configure opens",
    "gemini-cli": "~/.gemini/settings.json (merge into the existing object; Google replaced Gemini CLI with Antigravity CLI)",
    "antigravity-cli": "~/.gemini/config/mcp_config.json (all projects) or .agents/mcp_config.json (one project); merge into the existing object",
    "qwen-code": "settings.json (merge into the existing object)",
    "workbuddy": "~/.workbuddy/mcp.json or <project>/.workbuddy/mcp.json",
    "codebuddy": "~/.codebuddy/mcp.json or .mcp.json in the project",
    "trae": "Settings > MCP > Add > Manual > raw JSON, paste this (all projects); or .trae/mcp.json in one project",
    "warp": "Settings > Agents > MCP servers > Add, paste this",
    "cherry-studio": "Settings > MCP > MCP Servers > Add > Import from JSON, paste this (Cherry Studio 2)",
}
OTHER_CLIENTS = ["claude-code", "vscode", "zed", "opencode", "continue", "codex", "openclaw", "amp", "dsh"]
ALIASES = {"chatgpt-desktop": "codex", "codex-ide": "codex"}   # OpenAI's Codex CLI, IDE extension and the ChatGPT desktop app (which
#                                                                absorbed the Codex app in 2026) all read the one ~/.codex/config.toml
CLIENTS = sorted(list(MCPSERVERS_JSON) + OTHER_CLIENTS + list(ALIASES))
SURFACE = {                       # what the user runs, for the "which agent" question; same server, different config file
    "cli": ["claude-code", "codex", "antigravity-cli", "gemini-cli", "opencode", "qwen-code", "amp", "dsh", "openclaw"],
    "desktop": ["chatgpt-desktop", "claude-desktop", "cherry-studio", "workbuddy", "codebuddy", "warp"],
    "ide": ["cursor", "windsurf", "vscode", "zed", "cline", "continue", "kiro", "trae", "codex-ide"],
}
APPLY = {"command": ["claude-code"],                                                   # run one shell command
         "gui": ["claude-desktop", "cline", "trae", "warp", "cherry-studio"]}         # paste into the app's own MCP screen
APPLY["file"] = [c for c in CLIENTS if c not in APPLY["command"] + APPLY["gui"]]       # edit a config file


def apply_kind(client: str) -> str:
    return next(k for k, names in APPLY.items() if client in names)


def launch_command(source: str) -> tuple[str, list[str]]:
    if source == "pipx":
        return "hermind-mcp", []
    if source == "checkout":
        return sys.executable, [str(ROOT / "hermind_mcp.py")]
    return "uvx", ["--from", git_url(), "hermind-mcp"]


def _rq(path: str) -> str:
    """Quote a path for the REMOTE shell; a leading ~/ becomes "$HOME/..." so it expands over there, not here."""
    import shlex
    if path.startswith("~/"):
        rest = re.sub(r'([\\"$`])', r"\\\1", path[2:])
        return f'"$HOME/{rest}"'
    return shlex.quote(path)


def remote_launch(ns, env: dict) -> tuple[str, list[str]]:
    """The notes live on another machine: run the server there and speak MCP over ssh's stdin/stdout (no port, no new service)."""
    remote_env = dict(env)
    if ns.remote_hermes_profile:
        name = ns.remote_hermes_profile
        d = "~/.hermes" if name == "default" else f"~/.hermes/profiles/{name}"
        remote_env["HERMIND_DB"] = f"{d}/hermind.db"   # the server falls back to decision_memory.db when only that exists there
    prefix = "".join(f"{k}={_rq(v)} " for k, v in remote_env.items())
    server = f"{_rq(ns.remote_checkout.rstrip('/') + '/hermind_mcp.py')}"
    ssh = ["-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"]
    if ns.ssh_port:
        ssh += ["-p", str(ns.ssh_port)]
    if ns.ssh_key:
        ssh += ["-o", "IdentitiesOnly=yes", "-i", str(Path(ns.ssh_key).expanduser())]   # only this key: never the agent's (a normal key would open a shell)
    if ns.restricted:                              # the server's key is locked to one forced command: nothing to send
        return "ssh", ssh + [ns.remote]
    return "ssh", ssh + [ns.remote, f"{'env ' + prefix if prefix else ''}python3 {server}"]


def config_snippet(client: str, source: str, env: dict, launch: tuple[str, list[str]] | None = None) -> dict:
    cmd, args = launch or launch_command(source)
    j = lambda o: json.dumps(o, indent=2, ensure_ascii=False)          # noqa: E731
    server = {"command": cmd, "args": args, **({"env": env} if env else {})}
    if client in MCPSERVERS_JSON:
        return {"where": MCPSERVERS_JSON[client], "format": "json", "snippet": j({"mcpServers": {"hermind": server}})}
    if client == "claude-code":
        import shlex                          # every value that lands in a shell command line is quoted for the shell
        envs = "".join(f" -e {shlex.quote(f'{k}={v}')}" for k, v in env.items())
        return {"where": "run in a shell", "format": "shell",
                "snippet": f"claude mcp add hermind{envs} -- {' '.join(shlex.quote(x) for x in [cmd, *args])}"}
    if client == "vscode":
        return {"where": ".vscode/mcp.json or the user profile mcp.json", "format": "json",
                "snippet": j({"servers": {"hermind": {"type": "stdio", **server}}})}
    if client == "zed":
        return {"where": "Zed settings.json", "format": "json", "snippet": j({"context_servers": {"hermind": {**server, "env": env}}})}
    if client == "opencode":
        return {"where": "opencode.json", "format": "json", "snippet": j({"mcp": {"hermind": {
            "type": "local", "command": [cmd, *args], **({"environment": env} if env else {})}}})}
    if client == "amp":
        return {"where": "~/.config/amp/settings.json", "format": "json", "snippet": j({"amp.mcpServers": {"hermind": server}})}
    if client == "openclaw":
        return {"where": "openclaw.json (or: openclaw mcp set)", "format": "json", "snippet": j({"mcp": {"servers": {"hermind": server}}})}
    if client == "continue":
        lines = ["name: Hermind", "version: 0.0.1", "schema: v1", "mcpServers:", "  - name: hermind", f"    command: {json.dumps(cmd)}",
                 "    args: " + json.dumps(args)]
        if env:
            lines += ["    env:"] + [f"      {k}: {json.dumps(v)}" for k, v in env.items()]
        return {"where": ".continue/mcpServers/hermind.yaml (agent mode only; Continue is no longer maintained since Cursor acquired it in June 2026)", "format": "yaml", "snippet": "\n".join(lines)}
    if client == "codex":
        lines = ["[mcp_servers.hermind]", f"command = {json.dumps(cmd)}", f"args = {json.dumps(args)}"]
        if env:
            lines += ["[mcp_servers.hermind.env]"] + [f"{k} = {json.dumps(v)}" for k, v in env.items()]
        return {"where": "~/.codex/config.toml", "format": "toml", "snippet": "\n".join(lines)}
    if client == "dsh":
        lines = ["- insert:", "    - id: mcp-hermind", "      name: '@deepseek-ai/dsh-mcp-client'", "      config:", "        serverName: hermind",
                 "        transport: stdio", f"        command: {json.dumps(cmd)}", "        args: " + json.dumps(args)]
        if env:
            lines += ["        env:"] + [f"          {k}: {json.dumps(v)}" for k, v in env.items()]
        lines.append("        toolCallTimeoutMs: 30000")
        return {"where": "a Cordis patch file: dsh web --patch FILE", "format": "yaml", "snippet": "\n".join(lines)}
    raise KeyError(client)


def config_cmd(ns) -> tuple[dict, int]:
    if ns.list or not ns.client:
        return {"clients": CLIENTS, "groups_by_how_you_apply_the_config": APPLY, "groups_by_what_you_run": SURFACE, "untested_note": "only claude-code's CLI engine and the official MCP TypeScript SDK were run against the server; "
                "every other snippet follows the client's own documentation"}, 0
    client = ns.client.lower()
    if client not in CLIENTS:
        return {"error": f"unknown client '{ns.client}'", "clients": CLIENTS}, 2
    env: dict = {}
    if ns.remote and (ns.db_from_profile or ns.source != "git"):
        return {"error": "--remote runs the server on the other machine: use --remote-hermes-profile / --db / --key-file "
                         "(paths on THAT machine) instead of --db-from-profile / --source"}, 2
    if ns.restricted and not ns.remote:
        return {"error": "--restricted needs --remote USER@HOST"}, 2
    if ns.restricted and not ns.ssh_key:
        return {"error": "--restricted needs --ssh-key FILE: the dedicated key made by `hermind authorize-key`; without it ssh "
                         "would offer your default keys, which are not the restricted one (and may open a full shell)"}, 2
    if ns.restricted and (ns.db or ns.write or ns.trust_writes or ns.key_file or ns.remote_hermes_profile):
        return {"error": "--restricted: the forced command in authorized_keys fixes the database, key file and write mode; "
                         "set them with `hermind authorize-key` on the server instead"}, 2
    if ns.remote_hermes_profile and not ns.remote:
        return {"error": "--remote-hermes-profile needs --remote USER@HOST"}, 2
    if ns.db_from_profile:
        ns.db = str(default_db_file(resolve_home(argparse.Namespace(hermes_home=None, profile=ns.db_from_profile))))
    if ns.db:
        env["HERMIND_DB"] = ns.db if ns.remote else str(Path(ns.db).expanduser())
    if ns.trust_writes and not ns.write:
        return {"error": "--trust-writes only matters together with --write"}, 2
    if ns.write:
        env["HERMIND_MCP_WRITE"] = "1"
    if ns.trust_writes:
        env["HERMIND_MCP_TRUST_WRITES"] = "1"
    if ns.key_file:
        env["HERMIND_DB_KEY_FILE"] = ns.key_file if ns.remote else str(Path(ns.key_file).expanduser())
    launch = None
    if ns.remote:
        launch = remote_launch(ns, env)
        env = {}                                   # everything for the server travels inside the ssh command
    out = {"client": client, **config_snippet(ALIASES.get(client, client), ns.source, env, launch)}
    if client in ALIASES:
        out["note_alias"] = f"{client} reads the same file as {ALIASES[client]}: one entry covers the CLI, the IDE extension and the desktop app"
    out["surface"] = next((k for k, v in SURFACE.items() if client in v), "other")
    if ns.source == "git":
        out["note"] = f"installs the tagged release v{version()} from GitHub with uvx; --source checkout runs a local clone instead"
    if ns.source == "checkout":
        out["note"] = "points at this checkout and this Python; moving either breaks it"
    if ns.remote and ns.restricted:
        out["note"] = (f"{ns.remote}: the key is locked to Hermind by a forced command on the server; the client sends no command, "
                       "so a stolen key can only read (or write, if the line says so) the notes, never open a shell")
    elif ns.remote:
        out["note"] = (f"runs the server on {ns.remote} over ssh: the key must work without a password (BatchMode), "
                       f"{ns.remote_checkout} must hold a Hermind checkout there, and paths in --db/--key-file are paths on that machine")
    out["apply"] = apply_kind(client)
    out["tested"] = client == "claude-code"
    return out, 0


def config_text(d: dict) -> str:
    if d.get("error") and "snippet" not in d and "clients" not in d:
        return f"ERROR: {d['error']}"
    if "clients" in d and "snippet" not in d:
        return ((f"ERROR: {d['error']}; the known clients are:\n" if d.get("error") else "") + "\n".join(d.get("clients", [])) + ("\n" + d["untested_note"] if "untested_note" in d else ""))
    return f"# {d['client']}: {d['where']}\n{d['snippet']}" + (f"\n# note: {d['note']}" if d.get("note") else "")


# -- authorize-key: a ssh key that can only run Hermind --------------------------------------------------------------


def authorize_key(ns) -> dict:
    """Print the authorized_keys line for the machine that holds the notes. Never edits authorized_keys itself."""
    res: dict = {"ok": False}
    pub: str | None = None
    if ns.generate:
        key = Path(ns.generate).expanduser()
        if key.exists():
            res["error"] = f"{key} already exists: refusing to overwrite it (use --pubkey {key}.pub to reuse it)"
            return res
        if not shutil.which("ssh-keygen"):
            res["error"] = "ssh-keygen not found"
            return res
        key.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", ns.comment or "hermind-mcp", "-f", str(key)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            res["error"] = "ssh-keygen failed: " + r.stderr.strip()[:200]
            return res
        pub = Path(str(key) + ".pub").read_text().strip()
        res["private_key"] = str(key)
        res["note_key"] = "this private key has no passphrase and can only run Hermind once its line is in authorized_keys; copy it to the other machine"
    elif ns.pubkey:
        try:
            pub = Path(ns.pubkey).expanduser().read_text().strip()
        except OSError as exc:
            res["error"] = f"cannot read {ns.pubkey}: {exc.strerror}"
            return res
    if not pub or not re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+|sk-\S+) [A-Za-z0-9+/=]+( .*)?$", pub) or "\n" in pub:
        res["error"] = "need one public key line (ssh-ed25519 AAAA... comment): use --pubkey FILE.pub or --generate FILE"
        return res
    if ns.db:
        db = Path(ns.db).expanduser()
    else:
        db = default_db_file(resolve_home(argparse.Namespace(hermes_home=None, profile=ns.profile)))
    checkout = Path(ns.checkout).expanduser() if ns.checkout else ROOT
    server = checkout / "hermind_mcp.py"
    if not server.exists():
        res["error"] = f"{server} not found: point --checkout at the Hermind checkout on this machine"
        return res
    state = db_state(db)
    if state["state"] == "missing":
        res["error"] = f"{db} does not exist on this machine: pass --profile or --db"
        return res
    import shlex
    parts = [shlex.quote(ns.python or "python3"), shlex.quote(str(server)), "--db", shlex.quote(str(db))]
    if ns.write:
        parts.append("--write")
    if ns.trust_writes:
        if not ns.write:
            res["error"] = "--trust-writes only matters together with --write"
            return res
        parts.append("--trust-writes")
    cmd = " ".join(parts)
    if '"' in cmd or "\n" in cmd:
        res["error"] = "a path contains a double quote or newline: rename it"
        return res
    opts = "restrict,command=\"" + cmd + "\""
    res.update(ok=True, database=state, write=bool(ns.write), authorized_keys_line=f"{opts} {pub}",
               add_it=["append that one line to ~/.ssh/authorized_keys of the user on THIS machine (it does not replace anything)",
                       "on the other machine: hermind config CLIENT --remote USER@HOST --restricted --ssh-key FILE --json"],
               guarantees=["restrict: no shell, no pty, no port or agent forwarding, no X11", "the client's own command is ignored: the key can only start this server",
                           "read-only unless --write was given here"])
    if state["state"] == "encrypted":
        res["warning"] = "the database is encrypted: the forced command needs HERMIND_DB_KEY_FILE; set it with environment=... in sshd or wrap the command in a script"
    return res


# -- review and audit: the maintainer's side of the privacy controls ---------------------------------------------------


def _db_path(ns) -> Path:
    if getattr(ns, "db", None):
        return Path(ns.db).expanduser()
    return default_db_file(resolve_home(argparse.Namespace(hermes_home=None, profile=getattr(ns, "profile", None))))


def _ids(text: str) -> list[int]:
    """Note ids from "12,15", "12 15" or "#12, #15": positive whole numbers separated by commas and/or spaces, an optional
    # in front of each. Anything else (-2, 1-3, 1abc, 0, a number SQLite cannot hold) raises ValueError naming the item."""
    out = []
    for item in re.split(r"[,\s]+", (text or "").strip()):
        if not item:
            continue
        m = re.fullmatch(r"#?(\d+)", item)
        if not m or not 1 <= int(m.group(1)) < 2 ** 63:
            raise ValueError(f"{item!r} is not a note id (use positive whole numbers separated by commas or spaces, like 12,15)")
        out.append(int(m.group(1)))
    return out


def review_cmd(ns) -> dict:
    try:
        return _review_cmd(ns)
    except RuntimeError as e:
        if type(e).__name__ != "EncryptionError":
            raise
        return {"ok": False, "error": str(e)}
    except (OSError, sqlite3.Error) as e:
        return {"ok": False, "error": f"cannot use the database: {e}"}


def _review_cmd(ns) -> dict:
    """List, confirm or reject the notes waiting in the review queue (what MCP agents and the LLM extractor wrote)."""
    db = _db_path(ns)
    if not db.exists():
        return {"ok": False, "error": f"{db} does not exist: pass --profile or --db"}
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    store = DecisionStore(str(db))
    try:
        res: dict = {"ok": True, "database": str(db)}
        todo = []
        for flag, action in (("confirm", "confirm"), ("reject", "reject"), ("restore", "restore")):   # check every flag first: a bad one changes nothing
            val = getattr(ns, flag)
            if val is not None:
                if val.strip() == "all" and flag != "restore":
                    ids = [n["id"] for n in store.list_pending(limit=500)]
                else:
                    try:
                        ids = _ids(val)
                    except ValueError as exc:
                        return {"ok": False, "error": f"--{flag}: {exc}"}
                if not ids:
                    return {"ok": False, "error": f"--{flag} needs note ids (e.g. --{flag} 12,15)" + ("" if flag == "restore" else " or 'all'")}
                todo.append((action, ids))
        for action, ids in todo:
            res[action] = store.review_many(action, ids)
        pending = store.list_pending(limit=ns.limit)
        res["pending_count"] = store.pending_count()
        res["pending"] = [{"id": n["id"], "kind": n["kind"], "from": n.get("source_session") or "",
                           "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(n["created_at"])),
                           "text": (n["content"] or "")[:ns.chars]} for n in pending]
        return res
    finally:
        store.close()


def review_text(d: dict) -> str:
    if not d["ok"]:
        return "ERROR: " + d["error"]
    lines = []
    for act in ("confirm", "reject", "restore"):
        if act in d:
            lines.append(f"{act}: done {d[act]['done']}, skipped {d[act]['skipped']}")
    lines.append(f"{d['pending_count']} note(s) waiting for review in {d['database']}")
    for n in d["pending"]:
        lines.append(f"  #{n['id']} [{n['kind']}] {n['created']} from {n['from'] or '?'}: {n['text']}")
    if d["pending"]:
        lines.append("confirm: hermind review --confirm 12,15 (or all)   reject: hermind review --reject 12")
    return "\n".join(lines)


def audit_db(ns) -> dict:
    """Read-only scan of a database for notes that look like they hold a credential or personal data. Changes nothing.
    Excerpts are shown with personal data masked; a note with a credential-like value is listed without any excerpt."""
    db = _db_path(ns)
    if not db.exists():
        return {"ok": False, "error": f"{db} does not exist: pass --profile or --db"}
    sys.path.insert(0, str(ROOT))
    import dbdriver
    from pii import find_pii, redact_pii
    from screen import looks_masked_secret, looks_secret, looks_secret_label, strip_private
    try:
        if dbdriver.is_plaintext_file(db):
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        else:
            con = dbdriver.connect(str(db))
            con.execute("PRAGMA query_only = ON")
    except Exception as exc:
        return {"ok": False, "error": f"cannot open {db}: {type(exc).__name__}: {str(exc)[:160]}"}
    findings, counts, total = [], {}, 0
    entity_findings: list[dict] = []
    n_entities = 0
    try:
        for eid, name, aliases in con.execute("SELECT id, name, COALESCE(aliases,'') FROM entities ORDER BY id"):
            n_entities += 1                      # each name and each alias on its own, never joined with anything else
            where = [pos for pos, v in [("name", name), *((f"alias {i}", a) for i, a in enumerate(aliases.split("|"), 1))]
                     if v and looks_secret_label(v)]
            if where:
                entity_findings.append({"entity_id": eid, "found": ["credential"], "where": where})
        for nid, content, keywords, kind, status, review, created, tags in con.execute(
                "SELECT id, content, COALESCE(keywords,''), kind, status, review, created_at, COALESCE(tags,'') "
                "FROM notes ORDER BY id"):
            total += 1
            text = f"{content}\n{keywords}\n{tags}"           # a note is flagged for its own content, keywords and tags only
            kinds = []
            if looks_secret(content) or looks_secret(keywords) or any(looks_secret_label(t) for t in [tags, *tags.split()]):
                kinds.append("credential")
            if looks_masked_secret(text):
                kinds.append("masked-credential")
            kinds += [f"pii:{k}" for k in find_pii(text)]
            if strip_private(text)[1]:
                kinds.append("private-tag")
            if not kinds:
                continue
            for k in kinds:
                counts[k] = counts.get(k, 0) + 1
            item = {"id": nid, "kind": kind, "status": status, "review": review,
                    "created": time.strftime("%Y-%m-%d", time.localtime(created)), "found": kinds}
            if "credential" not in kinds:
                item["excerpt"] = redact_pii(content or "")[0][:ns.chars]
            findings.append(item)
    except sqlite3.Error as exc:       # a truncated or damaged file, or a SQLite file that is not a Hermind database
        return {"ok": False, "error": f"cannot read {db}: {type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        con.close()
    res = {"ok": True, "database": str(db), "notes_scanned": total, "flagged": len(findings), "by_type": counts,
           "findings": findings[:ns.limit], "shown": min(len(findings), ns.limit),
           "entities_scanned": n_entities, "entities_flagged": len(entity_findings),
           "entity_findings": entity_findings[:ns.limit],
           "note": "pattern matching only: expect some false positives and some misses (names, addresses and free text are not detected). "
                   "Nothing was changed. Remove a note with memory_forget, or edit it with memory_update."}
    if entity_findings:
        res["entity_advice"] = ("an ENTITY row (name or alias) looks like a credential; the value is not shown. Hermind has no command or tool "
                                "that edits or removes an entity alias, and memory_forget/memory_update only touch notes. To clear it, stop "
                                "every program that uses the database, keep a copy of the file, then run in the sqlite3 shell: "
                                "UPDATE entities SET aliases='' WHERE id=<entity_id>; (alias findings), or, for a name, rename it with "
                                "UPDATE entities SET name='renamed', key='renamed' WHERE id=<entity_id>; "
                                "the entity index is rebuilt from the table the next time the database is opened.")
    return res


def audit_text(d: dict) -> str:
    if not d["ok"]:
        return "ERROR: " + d["error"]
    lines = [f"{d['notes_scanned']} notes scanned in {d['database']}: {d['flagged']} flagged "
             + (", ".join(f"{k} {v}" for k, v in sorted(d["by_type"].items())) if d["by_type"] else "")]
    if d.get("entity_findings"):
        lines.append(f"{d['entities_scanned']} entities scanned: {d['entities_flagged']} flagged (ENTITY findings, the value is not shown)")
        lines += [f"  ENTITY #{e['entity_id']} {','.join(e['where'])}: {','.join(e['found'])}" for e in d["entity_findings"]]
        if d["entities_flagged"] > len(d["entity_findings"]):
            lines.append(f"  ... {d['entities_flagged'] - len(d['entity_findings'])} more (raise --limit)")
        lines.append(d["entity_advice"])
    for f in d["findings"]:
        lines.append(f"  #{f['id']} [{f['kind']}/{f['status']}/{f['review']}] {f['created']} {','.join(f['found'])}"
                     + (f": {f['excerpt']}" if f.get("excerpt") else "  (excerpt not shown: credential-like value)"))
    if d["flagged"] > d["shown"]:
        lines.append(f"  ... {d['flagged'] - d['shown']} more (raise --limit)")
    lines.append(d["note"])
    return "\n".join(lines)


# -- install-hook: automatic recall for Claude Code ---------------------------------------------------------------------

HOOK_MARK = "hermind-hook"          # how our entry is recognised again (the command contains it, or hermind_hook.py)


def _is_ours(command: str) -> bool:
    return "hermind-hook" in command or "hermind_hook.py" in command


def _own_bin() -> str:
    return os.path.join(sys.prefix, "Scripts" if os.name == "nt" else "bin")


def _path_without_own_bin() -> str:
    """PATH as another program (Claude Code) sees it: without the folder of the environment this CLI runs in, which
    `uvx` adds only for its own run."""
    own = os.path.normcase(os.path.abspath(_own_bin()))
    return os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if p and os.path.normcase(os.path.abspath(p)) != own)


def default_source() -> str:
    """How another program should start Hermind: from this checkout, from an installed command that stays on PATH
    (pip, pipx, `uv tool install`), or else through uvx from the pinned git tag (a `uvx --from` run leaves nothing on PATH)."""
    if (ROOT / "tools" / "deploy.sh").exists():
        return "checkout"
    return "pipx" if shutil.which("hermind-hook", path=_path_without_own_bin()) else "git"


def hook_command(ns, db: str | None) -> str:
    import shlex
    if ns.source == "pipx":
        parts = ["hermind-hook"]
    elif ns.source == "git":
        parts = ["uvx", "--from", git_url(), "hermind-hook"]
    else:
        parts = [sys.executable, str(ROOT / "hermind_hook.py")]
    if db:
        parts += ["--db", db]
    return " ".join(shlex.quote(p) for p in parts)


def install_hook(ns) -> dict:
    """Add (or with --remove, delete) a UserPromptSubmit hook in Claude Code's settings.json. Prints the change by default;
    --write applies it after saving a backup. Only the entry that runs hermind-hook is ever touched."""
    settings = Path(os.path.expanduser(ns.settings))
    if ns.db_from_profile:
        ns.db = str(default_db_file(resolve_home(argparse.Namespace(hermes_home=None, profile=ns.db_from_profile))))
    db = str(Path(ns.db).expanduser()) if ns.db else None
    ns.source = ns.source or default_source()
    cmd = hook_command(ns, db)
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() and settings.read_text(encoding="utf-8").strip() else {}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"cannot read {settings}: {exc}; fix or move it first (nothing was changed)"}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict) or not isinstance(data.get("hooks", {}).get("UserPromptSubmit", []), list):
        return {"ok": False, "error": f"{settings} has an unexpected hooks layout; nothing was changed"}
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("UserPromptSubmit", [])
    kept, removed = [], 0
    for g in groups:
        inner = [h for h in (g.get("hooks") or []) if isinstance(g, dict) and not _is_ours(str(h.get("command", "")))]
        removed += len(g.get("hooks") or []) - len(inner) if isinstance(g, dict) else 0
        if isinstance(g, dict) and inner:
            kept.append({**g, "hooks": inner})
        elif not isinstance(g, dict) or not g.get("hooks"):
            kept.append(g)
    if not ns.remove:
        kept.append({"hooks": [{"type": "command", "command": cmd, "timeout": ns.timeout}]})
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    if not hooks:
        data.pop("hooks", None)
    res: dict = {"ok": True, "settings": str(settings), "action": "remove" if ns.remove else "install", "command": cmd,
                 "replaced_existing_entries": removed, "written": False}
    if not ns.remove:
        res["snippet"] = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd, "timeout": ns.timeout}]}]}}, indent=2)
        res["probe"] = _probe_hook(cmd)
    if ns.write:
        if settings.exists():
            backup = settings.with_name(settings.name + time.strftime(".bak-hermind-%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}")
            shutil.copy2(settings, backup)
            os.chmod(backup, 0o600)
            res["backup"] = str(backup)
        settings.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.with_name(settings.name + ".hermind-tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600 if not settings.exists() else (settings.stat().st_mode & 0o777))
        os.replace(tmp, settings)
        res["written"] = True
        res["next"] = ("restart Claude Code so it stops using the hook" if ns.remove else
                       "restart Claude Code (or open /hooks) so it reads the new setting; undo with `hermind install-hook --remove --write`")
    else:
        res["next"] = "this was a preview: add --write to apply it (a backup of settings.json is saved first)"
    return res


def _probe_hook(cmd: str) -> dict:
    """Run the hook once on a harmless prompt, the way Claude Code does, and report what happened."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "what do you remember about hermind"}),
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PATH": _path_without_own_bin()})   # as Claude Code would start it
    except subprocess.TimeoutExpired:
        return {"ran": False, "problem": "timed out after 30 s"}
    ms = int((time.time() - t0) * 1000)
    out = r.stdout.strip()
    ok_json = False
    notes = 0
    if out:
        try:
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            ok_json, notes = True, sum(1 for line in ctx.splitlines() if line.startswith("#"))
        except (ValueError, KeyError):
            pass
    return {"ran": r.returncode == 0, "exit_code": r.returncode, "milliseconds": ms, "returned_valid_context": ok_json,
            "notes_in_context": notes,
            "note": "no notes matched the probe prompt (normal for an empty or unrelated database)" if r.returncode == 0 and not out else "",
            **({"problem": (r.stderr.strip().splitlines() or ["no output"])[-1][:300]} if r.returncode else {})}


def install_hook_text(d: dict) -> str:
    if not d["ok"]:
        return "ERROR: " + d["error"]
    lines = [f"{d['action']}: {d['settings']}"]
    if d.get("command") and d["action"] == "install":
        lines.append(f"hook command: {d['command']}")
        p = d.get("probe") or {}
        lines.append(f"probe: ran={p.get('ran')} in {p.get('milliseconds')} ms, valid context={p.get('returned_valid_context')}, notes={p.get('notes_in_context')}"
                     + (f" ({p['note']})" if p.get("note") else "") + (f" PROBLEM: {p['problem']}" if p.get("problem") else ""))
    if d.get("backup"):
        lines.append(f"backup: {d['backup']}")
    lines.append(("written. " if d["written"] else "") + d["next"])
    return "\n".join(lines)


# -- questions to ask before installing -----------------------------------------------------------------------------


def questions() -> dict:
    """Questions for the user, in order, each with the answers that make it relevant. Choices come from this machine."""
    d = doctor()
    allp = d["hermes_profiles"]
    profs = [p for p in allp if p["plugin_version"] or p["db"]["state"] != "missing"] or (allp if d["commands"]["hermes"] else [])
    clients = d["clients_detected"]
    default_prof = next((p for p in profs if p["name"] == "default"), None)
    qs: list[dict] = []
    hermes_first = bool(profs)
    t = [{"value": "hermes", "label": "Hermes agent",
          "detail": "automatic recall before every turn and automatic capture (the full experience); CLI, desktop app and gateway share it"},
         {"value": "mcp-cli", "label": "A command-line agent through MCP",
          "detail": "Claude Code, Codex CLI, Antigravity CLI, Gemini CLI, OpenCode, Qwen Code, Amp, dsh: the agent calls the memory tools itself"},
         {"value": "mcp-app", "label": "A desktop app or IDE through MCP",
          "detail": "ChatGPT/Codex desktop, Claude Desktop, Cursor, VS Code, Windsurf, Zed, Cline, Kiro, WorkBuddy, Cherry Studio..."},
         {"value": "several", "label": "Several of these", "detail": "e.g. Hermes plus an MCP agent, all on the same notes"}]
    if not hermes_first:
        t = [t[1], t[2], t[0], t[3]]
    qs.append({"id": "target", "question": "Where should Hermind be installed?", "choices": t, "multi_select": False})
    HER, MCP = ["hermes", "several"], ["mcp-cli", "mcp-app", "several"]
    qs.append({"id": "hermes_where", "when": {"target": HER}, "multi_select": False,
               "question": "Where does your Hermes agent actually run? (its memory plugin belongs on that machine)",
               "choices": [{"value": "here", "label": "On this machine",
                            "detail": "Hermes CLI, a desktop app using a local backend, and the gateway all load the same plugin folder"},
                           {"value": "remote", "label": "On another machine (a VPS) my desktop app connects to",
                            "detail": "run the install there (ssh); the desktop app on your own computer needs nothing for Hermind"}]})
    if profs:
        qs.append({"id": "profiles", "when": {"target": HER}, "multi_select": True,
                   "question": "Which Hermes profiles should get it? (a profile is usually one person; each has its own database)",
                   "choices": [{"value": p["name"], "label": p["name"],
                                "detail": f"plugin {p['plugin_version'] or 'not installed'}, database {p['db']['state']}"
                                          + (f", {p['db']['notes']} notes" if "notes" in p["db"] else "")} for p in profs][:4]})

    def client_choices(kinds):
        pool = [c for k in kinds for c in SURFACE[k]]
        seen = [c for c in clients if c in pool]
        pick = (seen + [c for c in pool if c not in seen])[:4]
        return [{"value": c, "label": c, "detail": "detected on this machine" if c in clients else "not detected here"} for c in pick]

    qs.append({"id": "client_cli", "when": {"target": ["mcp-cli", "several"]}, "multi_select": True,
               "question": "Which command-line agent(s) should connect? (another name: type it under Other; `hermind config --list`)",
               "choices": client_choices(["cli"])})
    qs.append({"id": "client_app", "when": {"target": ["mcp-app", "several"]}, "multi_select": True,
               "question": "Which desktop app(s) or IDE(s) should connect? (another name: type it under Other)",
               "choices": client_choices(["desktop", "ide"])})
    if default_prof and default_prof["db"]["state"] in ("plain", "encrypted"):
        qs.append({"id": "database", "when": {"target": MCP}, "multi_select": False,
                   "question": "Which notes should the MCP agent(s) see?",
                   "choices": [{"value": "shared", "label": "The same notes as my Hermes default profile",
                                "detail": f"{default_prof['db']['path']}: the MCP agent runs on this machine too"},
                               {"value": "shared-remote", "label": "The Hermes notes, but they live on another machine",
                                "detail": "reached over ssh: `config CLIENT --remote USER@HOST --remote-hermes-profile NAME` (ask for user@host and the key)"},
                               {"value": "separate", "label": "A new, separate memory",
                                "detail": "~/.hermind/memory.db, starts empty, shares nothing with Hermes"}]})
    qs.append({"id": "write", "when": {"target": MCP}, "multi_select": False,
               "question": "May the MCP agent(s) save notes, or only read them?",
               "choices": [{"value": "read-only", "label": "Read only (recommended)", "detail": "search and open notes; nothing can be changed"},
                           {"value": "read-write", "label": "Read and save", "detail": "also remember and update notes (--write)"}]})
    return {"detected": {"hermes_profiles": [p["name"] for p in profs], "clients": clients, "hermind_version": d["hermind"]["version"],
                         "is_checkout": d["hermind"]["is_checkout"]},
            "questions": qs,
            "after_the_answers": ["run the matching install-hermes / config command with --dry-run or --json and show the plan",
                                  "ask once more: apply this plan? (yes / change something / cancel)", "only then apply"]}


def questions_text(d: dict) -> str:
    lines = []
    for i, q in enumerate(d["questions"], 1):
        when = f"   [only if target is {' or '.join(q['when']['target'])}]" if q.get("when") else ""
        lines.append(f"{i}. {q['question']}{when}")
        lines += [f"     - {c['value']}: {c['label']}  ({c['detail']})" for c in q["choices"]]
    return "\n".join(lines)


def install_skill(ns) -> dict:
    src = ROOT / "skills" / "hermind-setup"
    if not (src / "SKILL.md").exists():
        return {"ok": False, "error": "skills/hermind-setup/SKILL.md not found: run this from a git checkout"}
    home = Path.home()
    targets = {"hermes": Path(os.environ["HERMES_HOME"]).expanduser() / "skills" if os.environ.get("HERMES_HOME") else home / ".hermes" / "skills",
               "claude-code": home / ".claude" / "skills"}
    if ns.target == "both":                       # only agents that are installed here; an explicit --target always works
        chosen = [n for n in targets if targets[n].parent.exists()]
        if not chosen:
            return {"ok": False, "error": "neither Hermes (~/.hermes) nor Claude Code (~/.claude) is set up for this user; "
                                          "pass --target hermes or --target claude-code"}
    else:
        chosen = [ns.target]
    results = []
    for name in chosen:
        dest = targets[name] / "hermind-setup"
        same = dest.exists() and (dest / "SKILL.md").read_bytes() == (src / "SKILL.md").read_bytes()
        action = "already current" if same else ("update (old copy kept as .bak)" if dest.exists() else "install")
        results.append({"target": name, "path": str(dest), "action": action})
        if ns.dry_run or same:
            continue
        if dest.exists():
            bak = dest.with_name("hermind-setup.bak")
            shutil.rmtree(bak, ignore_errors=True)
            dest.rename(bak)
        shutil.copytree(src, dest)
    return {"ok": True, "dry_run": ns.dry_run, "results": results,
            "next": ["start a new session of that agent so it picks the skill up"] if not ns.dry_run else []}


# -- entry ---------------------------------------------------------------------------------------------------------


# -- plain command line use: try it without an agent, and take your data out --------------------------------------------


def _mcp_db(ns) -> Path:
    p = Path(os.path.expanduser(getattr(ns, "db", None) or os.environ.get("HERMIND_DB", "~/.hermind/memory.db")))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from store_common import with_legacy_fallback
    return with_legacy_fallback(p)


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def note_cmd(ns) -> dict:
    """remember / search / export, with file-system and database problems reported as one line instead of a traceback."""
    try:
        return _note_cmd(ns)
    except RuntimeError as e:      # dbdriver.EncryptionError: encrypted without a key, or not a database
        if type(e).__name__ != "EncryptionError":
            raise
        return {"ok": False, "error": str(e)}
    except (OSError, sqlite3.Error) as e:
        what = getattr(e, "filename", None) or str(_mcp_db(ns))
        return {"ok": False, "error": f"cannot use {what}: {e.strerror if isinstance(e, OSError) and e.strerror else e}"}


def _note_cmd(ns) -> dict:
    """remember / search / export against the MCP database (default ~/.hermind/memory.db, or HERMIND_DB, or --db)."""
    db = _mcp_db(ns)
    sys.path.insert(0, str(ROOT))
    from store import DecisionStore
    from store_common import explicit_never_expires
    if ns.cmd != "remember" and not db.exists():
        return {"ok": False, "error": f"{db} does not exist yet: save a note first (hermind remember \"...\")"}
    if not db.parent.exists():                     # a folder made here is the owner's only, as DecisionStore makes it
        db.parent.mkdir(parents=True, mode=0o700)
        os.chmod(db.parent, 0o700)
    store = DecisionStore(str(db))
    try:
        if ns.cmd == "remember":
            try:
                # a note saved on purpose with kind auto keeps the kind the text suggests but never expires;
                # only a kind chosen with --kind chat / news keeps its time to live
                nid, created = store.add(ns.text, kind=ns.kind, keywords=ns.keywords or "",
                                         never_expires=explicit_never_expires(ns.kind))
            except Exception as e:   # RefusedWrite (secret, forgotten text) or ValueError (empty text)
                return {"ok": False, "error": str(e) or type(e).__name__}
            note = store.get(nid) or {}
            exp = note.get("expires_at")
            return {"ok": True, "database": str(db), "id": nid, "created": created, "kind": note.get("kind"),
                    "expires": time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else None}
        if ns.cmd == "search":
            hits = store.search(ns.query, limit=ns.limit)
            return {"ok": True, "database": str(db), "results": [
                {"id": h["id"], "kind": h.get("kind"), "content": h.get("content")} for h in hits]}
        import export_notes
        try:
            res = export_notes.run_export(store, fmt=ns.format, out=ns.out, force=ns.force, live_only=ns.live_only,
                                          include_history=ns.include_history, by=ns.by, scope=ns.scope)
        except export_notes.ExportError as e:
            return {"ok": False, "error": str(e)}
        if "text" in res:
            print(res.pop("text"))
            return {"ok": True, "_quiet": True}
        return {"database": str(db), **res}
    finally:
        store.close() if hasattr(store, "close") else None


# -- hermind import: bring notes from somewhere else into the database -----------------------------------------------------


def import_cmd(ns) -> dict:
    """`hermind import` (preview, --apply, --list, --revert, --readers) with every failure reported as one line."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import import_core
    try:
        if ns.readers:
            return {"ok": True, "readers": import_core.reader_list()}
        db = _mcp_db(ns)
        if ns.list:
            return import_core.run_list(db)
        if ns.revert:
            return import_core.run_revert(ns.revert, db, apply=ns.apply, backup=not ns.no_backup)
        if not ns.source:
            return {"ok": False, "error": "give a SOURCE to import (a folder or file), or use --list / --revert BATCH / --readers"}
        return import_core.run_import(ns.source, db, reader=ns.from_reader, apply=ns.apply, exclude=ns.exclude or (),
                                      backup=not ns.no_backup,
                                      options={"encoding": ns.encoding, "ignore_file_names": ns.ignore_file_names,
                                               "include_removed": ns.include_removed})
    except import_core.ImportFailed as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:      # dbdriver.EncryptionError: encrypted without a key, or not a database
        if type(e).__name__ != "EncryptionError":
            raise
        return {"ok": False, "error": str(e)}
    except (OSError, sqlite3.Error) as e:
        what = getattr(e, "filename", None) or str(_mcp_db(ns))
        return {"ok": False, "error": f"cannot use {what}: {e.strerror if isinstance(e, OSError) and e.strerror else e}"}


OUTCOME_ORDER = ("new", "version", "duplicate", "edited", "history", "refused", "unreadable", "empty", "excluded")   # = import_core.OUTCOMES


def _backup_line(d: dict) -> str:
    n = d.get("backup_removed") or 0
    return f"backup: {d['backup']}" + (f" ({n} older backup{'s' if n != 1 else ''} removed; the newest 5 are kept)" if n else "")


def _sq(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def import_text(d: dict) -> str:
    if not d["ok"]:
        return "ERROR: " + d["error"]
    if "readers" in d:
        width = max((len(r["name"]) for r in d["readers"]), default=0)
        return "\n".join(f"{r['name']:<{width}}  {r['accepts']}" for r in d["readers"]) or "no readers"
    if "batches" in d:
        if not d["batches"]:
            return f"no imports in {d['database']}"
        lines = []
        for b in d["batches"]:
            c = b["counts"]
            lines.append(f"{b['batch']}  {b['state']:<15} {b['created']}  reader {b['reader']}  from {b['source']}")
            lines.append(f"    {b['notes_remaining']} of {b['notes_created']} note(s) still there; "
                         + ", ".join(f"{c.get(k, 0)} {k}" for k in OUTCOME_ORDER))
        return "\n".join(lines)
    apply = d["mode"] == "apply"
    if "remove" in d:                                             # --revert
        lines = [("REVERTED" if apply else "PREVIEW (nothing was changed; add --apply to do this)") +
                 f" - batch {d['batch']} in {d['database']}"]
        if d.get("backup"):
            lines.append(_backup_line(d))
        for word, key in (("remove" if not apply else "removed", "remove"), ("keep", "keep"), ("already gone", "gone")):
            for i in d[key]:
                lines.append(f"  {word:<12} #{i['note_id']}  {i['origin']}" + (f"  - {i['reason']}" if i.get("reason") else ""))
        lines.append(f"{len(d['remove'])} {'removed' if apply else 'to remove'}, {len(d['keep'])} kept, {len(d['gone'])} already gone")
        if apply and d.get("entities_removed"):
            lines.append(f"also taken back from the entity index: {d['entities_removed']} entit{'y' if d['entities_removed'] == 1 else 'ies'} "
                         f"that only this import's notes used")
        return "\n".join(lines)
    lines = [("APPLIED" if apply else "PREVIEW (nothing was changed; add --apply to import)") +
             f" - {d['source']} into {d['database']}",
             f"reader: {d['reader']}" + (" (chosen from the shape of the source; --from NAME overrides)" if d["reader_detected"] else "")]
    for r in d["remarks"]:
        lines.append(f"note: {r}")
    if d.get("backup"):
        lines.append(_backup_line(d))
    elif apply and d.get("backup_note"):
        lines.append(f"backup: {d['backup_note']}")
    for e in d.get("entries", []):
        will = {"new": "imported" if apply else "will import",
                "version": "imported" if apply else "will import"}.get(e["outcome"], e["outcome"])
        detail = e["reason"] if e["outcome"] not in ("new", "version") else e.get("text", "")
        if e["outcome"] == "version":
            detail = f"(older version of its note) {detail}"
        if e["outcome"] in ("new", "version") and e["reason"]:
            detail = f"{detail}  ({e['reason']})"
        lines.append(f"  {will:<11} {e['origin']}" + (f"  {detail}" if detail else ""))
    for c in d.get("changed_since_preview", []):
        lines.append(f"  changed while importing: {c['origin']} became {c['now']} ({c['reason']})")
    c = d["counts"]
    lines.append(f"{d['total']} entries: " + ", ".join(f"{c[k]} {k}" for k in OUTCOME_ORDER))
    groups = d.get("groups") or []
    if len(groups) >= 2:
        lines.append("By source:")
        for g in groups:
            nums = ", ".join(f"{g['counts'][k]} {k}" for k in OUTCOME_ORDER if g["counts"][k])
            leave = f"   (leave out: --exclude {_sq(g['exclude'])})" if g.get("exclude") else ""
            lines.append(f"  {g['name']}: {nums or 'nothing'}{leave}")
    if apply:
        lines.append(f"batch {d['batch']}: {d['created']} note(s) created" if d["batch"] else "nothing to import, nothing was written")
        if d["batch"]:
            lines.append(f"to take it back: hermind import --revert {d['batch']} --apply")
    return "\n".join(lines)


def share_safe(d: dict) -> dict:
    """A copy of the doctor report that is safe to paste into a public issue: no home directory or user name, no
    executable paths, anonymous profile names. Counts and versions stay (they are what a maintainer needs)."""
    home = str(Path.home())
    names: dict[str, str] = {}

    def clean(v):
        if isinstance(v, str):
            for n, alias in names.items():
                v = v.replace(n, alias)
            return v.replace(home, "~")
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return v

    for i, p in enumerate(d.get("hermes_profiles") or [], 1):
        if isinstance(p, dict) and p.get("name"):
            names[p["name"]] = f"profile-{i}"
    out = clean(d)
    out["commands"] = {k: bool(v) for k, v in (d.get("commands") or {}).items()}
    out["python"] = {"version": (d.get("python") or {}).get("version")}
    out["hermind"] = {"version": d["hermind"]["version"], "is_checkout": d["hermind"]["is_checkout"]}
    out["shared_safely"] = "home paths, user name, profile names and executable paths were removed; counts and versions kept"
    return out


def vectors_cmd(ns) -> dict:
    """setup-vectors and embed-all (see vec_setup.py)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import vec_setup
    if ns.cmd == "setup-vectors":
        if ns.dry_run:
            return {"ok": True, "dry_run": True, **vec_setup.plan(ns.profile, ns.home)}
        return vec_setup.setup(ns.profile, ns.home, log=lambda m: print(f"... {m}", file=sys.stderr))
    from store_common import with_legacy_fallback
    db = with_legacy_fallback(Path(os.path.expanduser(ns.db or os.environ.get("HERMIND_DB") or "~/.hermind/memory.db")))
    if not db.is_file():
        return {"ok": False, "usage": True, "error": f"no database at {db}: pass --db FILE"}
    cfg = {**({"profile": ns.profile} if ns.profile else {}), **({"home": ns.home} if ns.home else {})}
    shown = []

    def progress(m):
        shown.append(m)
        print(f"\r{m}", end="", file=sys.stderr, flush=True)
    try:
        return vec_setup.embed_all(str(db), cfg, log=progress)
    except Exception as exc:                       # not a database, wrong key...: one line, not a traceback
        return {"ok": False, "usage": True, "error": f"cannot embed {db}: {exc}"}
    finally:
        if shown:
            print(file=sys.stderr)                 # end the progress line before the answer


def vectors_text(d: dict) -> str:
    import vec_setup
    return vec_setup.describe(d, cli="hermind" if not (ROOT / "tools" / "deploy.sh").exists() else "python3 hermind_cli.py")


def main(argv=None) -> int:
    """The command line. A closed pipe (`hermind export | head -1`) ends the command quietly instead of with a traceback."""
    try:
        code = _main(argv)
        try:
            if sys.stdout is not None:     # None: stdout was closed (`>&-`); the command ran, there is just nowhere to print
                sys.stdout.flush()         # buffered output meets the closed pipe here, not at interpreter exit
        except BrokenPipeError:
            raise
        except (ValueError, OSError):      # a closed or unusable stdout is not a failure of the command
            pass
        return code
    except BrokenPipeError:
        try:                               # what is left to flush at exit goes nowhere (the pattern of the Python documentation)
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 1


def _main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to a legacy code page that cannot print notes
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="hermind", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    for name in ("doctor", "selftest"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
        if name == "doctor":
            s.add_argument("--share", action="store_true", help="remove paths, user and profile names (safe to paste into an issue); implies --json")
    s = sub.add_parser("install-hermes")
    s.add_argument("--profile")
    s.add_argument("--hermes-home")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--rollback", nargs="?", const=True, metavar="BACKUP_DIR")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("config")
    s.add_argument("client", nargs="?")
    s.add_argument("--list", action="store_true")
    s.add_argument("--db")
    s.add_argument("--db-from-profile", metavar="NAME", help="use that Hermes profile's database (shared memory)")
    s.add_argument("--write", action="store_true")
    s.add_argument("--trust-writes", action="store_true", help="what the agent writes counts as confirmed memory at once")
    s.add_argument("--key-file")
    s.add_argument("--source", choices=("git", "checkout", "pipx"), default="git")
    s.add_argument("--remote", metavar="USER@HOST", help="the notes live on that machine: reach them over ssh")
    s.add_argument("--remote-checkout", default="~/hermind", help="Hermind checkout on the remote machine")
    s.add_argument("--remote-hermes-profile", metavar="NAME", help="use that Hermes profile's database on the remote machine")
    s.add_argument("--ssh-port", type=int)
    s.add_argument("--ssh-key")
    s.add_argument("--restricted", action="store_true", help="the remote key was made with `hermind authorize-key` (forced command); needs --ssh-key")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("authorize-key")
    s.add_argument("--pubkey")
    s.add_argument("--generate", metavar="KEYFILE")
    s.add_argument("--comment")
    s.add_argument("--profile")
    s.add_argument("--db")
    s.add_argument("--checkout")
    s.add_argument("--python")
    s.add_argument("--write", action="store_true")
    s.add_argument("--trust-writes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("install-hook")
    s.add_argument("--db")
    s.add_argument("--db-from-profile", metavar="NAME")
    s.add_argument("--source", choices=["checkout", "pipx", "git"], default=None,
                   help="how Claude Code starts the hook: this checkout, an installed `hermind-hook` command (pipx), or uvx "
                        "from the release tag (git). Default: checkout here, pipx if `hermind-hook` is on PATH, else git")
    s.add_argument("--settings", default="~/.claude/settings.json")
    s.add_argument("--timeout", type=int, default=10, help="seconds Claude Code waits before ignoring the hook")
    s.add_argument("--write", action="store_true")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("review")
    s.add_argument("--profile")
    s.add_argument("--db")
    s.add_argument("--confirm", metavar="IDS|all")
    s.add_argument("--reject", metavar="IDS|all")
    s.add_argument("--restore", metavar="IDS")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--chars", type=int, default=160)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("audit-db")
    s.add_argument("--profile")
    s.add_argument("--db")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--chars", type=int, default=100)
    s.add_argument("--fail-on-findings", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("remember", help="save a note to the MCP database")
    s.add_argument("text")
    s.add_argument("--kind", default="auto")
    s.add_argument("--keywords", help="5-10 alternative phrasings, space separated (helps search)")
    s = sub.add_parser("search", help="search the MCP database")
    s.add_argument("query")
    s.add_argument("--limit", type=_positive_int, default=5)
    s = sub.add_parser("export", help="hand over every note (all statuses, with entities and links) as JSON",
                       description=EXPORT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--format", choices=("json", "jsonl", "markdown"), default="json",
                   help="json (default): one record per note, every field; jsonl: entity and observation lines; "
                        "markdown: a folder of files, one per note (needs --out DIR)")
    s.add_argument("--by", choices=("kind", "entity"), default="kind", help="markdown: folders by kind (default) or by entity")
    s.add_argument("--out", help="write to this file (mode 600) instead of printing; for markdown, the folder")
    s.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    s.add_argument("--live-only", action="store_true",
                   help="json: only notes with status active (every field is still written)")
    s.add_argument("--include-history", action="store_true",
                   help="jsonl and markdown: also export older versions (superseded and merged notes), marked as such")
    s.add_argument("--scope", help="only this scope (default: all)")
    for name in ("remember", "search", "export"):
        sub.choices[name].add_argument("--db", help="database file (default: $HERMIND_DB or ~/.hermind/memory.db)")
        sub.choices[name].add_argument("--json", action="store_true")
    s = sub.add_parser("import", formatter_class=argparse.RawDescriptionHelpFormatter,
                       help="bring notes from a folder or file into the database (preview first)",
                       description=IMPORT_HELP)
    s.add_argument("source", nargs="?", metavar="SOURCE", help="a folder or file to import from")
    s.add_argument("--from", dest="from_reader", metavar="READER",
                   help="which reader to use (default: chosen from the shape of the source; --readers lists them)")
    s.add_argument("--apply", action="store_true",
                   help="really do it. Without --apply an import (or --revert) only shows what it would do")
    s.add_argument("--exclude", action="append", metavar="PATTERN",
                   help="skip entries whose origin (shown in the preview, e.g. notes/old/a.md) matches this glob; repeatable")
    s.add_argument("--no-backup", action="store_true", help="with --apply: do not copy the database first")
    s.add_argument("--encoding", metavar="NAME",
                   help="text and Markdown folders: the encoding of files that are not valid UTF-8, e.g. big5, gb18030, cp1252, "
                        "shift_jis (UTF-8 and UTF-16/32 with a byte-order mark are always read; without this option such "
                        "a file is listed as 'unreadable')")
    s.add_argument("--ignore-file-names", action="store_true",
                   help="text and Markdown folders: do not add the file name in front of a note that does not say it "
                        "(a title or name in the file's front matter is still used)")
    s.add_argument("--include-removed", action="store_true",
                   help="chatgpt-export only: also import what ChatGPT's data shows you removed or changed later (old memory "
                        "list items, memory writes that are not in the newest list, older custom instructions); by default "
                        "they are listed as 'history' and not imported")
    s.add_argument("--list", action="store_true", help="show the imports made so far (batches) and whether each is complete")
    s.add_argument("--revert", metavar="BATCH", help="take an import back: remove the notes it created (preview unless --apply)")
    s.add_argument("--readers", action="store_true", help="list the kinds of source that can be imported")
    s.add_argument("--db", help="database file (default: $HERMIND_DB or ~/.hermind/memory.db)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("questions")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("rename-db", help="rename decision_memory.db to hermind.db in a Hermes home (dry run unless --apply)")
    s.add_argument("--profile")
    s.add_argument("--hermes-home")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("setup-vectors", help="set up the optional semantic search: a private venv and one local model",
                       description="One-time setup of the optional semantic-search worker. Everything goes into one folder "
                                   "($HERMIND_VEC_HOME, else ~/.hermes/hermind-vec); delete it to remove everything. Needs the "
                                   "network once (PyPI and Hugging Face); afterwards nothing is sent anywhere.")
    s.add_argument("profile", nargs="?", default="default", choices=("default", "light", "balanced", "quality"),
                   help="light: 52 MB, ~160 MB RAM; default: 118 MB, ~480 MB; balanced: 152 MB, ~600 MB; quality: 568 MB, ~910 MB")
    s.add_argument("--home", help="the folder (default $HERMIND_VEC_HOME or ~/.hermes/hermind-vec)")
    s.add_argument("--dry-run", action="store_true", help="show what would be downloaded and where, change nothing")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("embed-all", help="give every existing note a vector (after setup-vectors or an import)")
    s.add_argument("--db", help="database file (default: $HERMIND_DB or ~/.hermind/memory.db)")
    s.add_argument("--profile", choices=("default", "light", "balanced", "quality"),
                   help="the model profile (default: the one installed; needed when several are)")
    s.add_argument("--home", help="the setup-vectors folder (default $HERMIND_VEC_HOME or ~/.hermes/hermind-vec)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("install-skill")
    s.add_argument("--target", choices=("hermes", "claude-code", "both"), default="both")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    ns = p.parse_args(argv)
    if ns.version:
        print(f"hermind {version()}")
        return 0
    if ns.cmd == "doctor":
        d = doctor()
        if ns.share:
            d, ns.json = share_safe(d), True
        emit(d, ns.json, doctor_text)
        return 0 if d["ok"] else 1
    if ns.cmd == "selftest":
        d = selftest()
        emit(d, ns.json, selftest_text)
        return 0 if d["ok"] else 1
    if ns.cmd == "install-hermes":
        d = rollback_hermes(ns) if ns.rollback else install_hermes(ns)
        emit(d, ns.json, install_text if not ns.rollback else lambda x: json.dumps(x, ensure_ascii=False, indent=2))
        return 0 if d["ok"] else (2 if d.get("error", "").startswith(("this is not", "/", "checkout")) else 1)
    if ns.cmd == "rename-db":
        d = rename_db(ns)
        emit(d, ns.json, rename_db_text)
        return 0 if d["ok"] else 2
    if ns.cmd == "authorize-key":
        d = authorize_key(ns)
        emit(d, ns.json, lambda x: x["authorized_keys_line"] + "\n" + "\n".join("# " + n for n in x["add_it"]) if x["ok"] else "ERROR: " + x["error"])
        return 0 if d["ok"] else 2
    if ns.cmd == "install-hook":
        d = install_hook(ns)
        emit(d, ns.json, install_hook_text)
        return 0 if d["ok"] else 2
    if ns.cmd == "review":
        d = review_cmd(ns)
        emit(d, ns.json, review_text)
        return 0 if d["ok"] else 2
    if ns.cmd == "audit-db":
        d = audit_db(ns)
        emit(d, ns.json, audit_text)
        return 2 if not d["ok"] else (1 if ns.fail_on_findings and (d["flagged"] or d["entities_flagged"]) else 0)
    if ns.cmd == "questions":
        emit(questions(), ns.json, questions_text)
        return 0
    if ns.cmd == "install-skill":
        d = install_skill(ns)
        emit(d, ns.json, lambda x: json.dumps(x, ensure_ascii=False, indent=2))
        return 0 if d["ok"] else 2
    if ns.cmd in ("remember", "search", "export"):
        d = note_cmd(ns)
        if d.get("_quiet"):
            return 0
        if ns.json or not d["ok"]:
            emit(d, ns.json, lambda x: "ERROR: " + x["error"])
        elif ns.cmd == "search":
            print("\n".join(f"#{r['id']} [{r['kind']}] {r['content']}" for r in d["results"]) or "no results")
        elif ns.cmd == "remember":
            life = f"expires {d['expires']}" if d.get("expires") else "does not expire"
            print((f"saved note #{d['id']}" if d["created"] else f"already saved as note #{d['id']}")
                  + f" (kind: {d['kind']}, {life})")
        else:
            import export_notes
            more = f" and {d['relations']} relations" if "relations" in d else ""
            into = f"into {d['folders']} folder(s) under" if "folders" in d else "to"
            print(f"exported {d['exported']} notes{more} {into} {d['file']} ({export_notes.status_text(d['by_status'])})")
        return 0 if d["ok"] else 2
    if ns.cmd in ("setup-vectors", "embed-all"):
        d = vectors_cmd(ns)
        emit(d, ns.json, vectors_text)
        return 0 if d["ok"] else (2 if d.get("usage") else 1)
    if ns.cmd == "import":
        d = import_cmd(ns)
        emit(d, ns.json, import_text)
        return 0 if d["ok"] else 2
    if ns.cmd == "config":
        d, code = config_cmd(ns)
        emit(d, ns.json, config_text)
        return code
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
