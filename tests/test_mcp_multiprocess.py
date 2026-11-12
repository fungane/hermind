"""Several agent processes share one SQLite file: MCP writers, an MCP reader and a process that uses the store directly
the way the Hermes plugin does (start-up maintenance + captures). None may fail with 'database is locked'."""

import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLUGIN_LIKE = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from store import DecisionStore
db, n = sys.argv[2], int(sys.argv[3])
errors = 0
for round_ in range(n):
    s = DecisionStore(db, track_access=True)            # a plugin session starts: schema check + maintenance
    try:
        s.maintenance(consolidate=True)
        for i in range(5):
            s.add(f"plugin session {round_} capture {i} about nginx rate limits and backups", kind="chat",
                  source_session=f"plugin-{round_}")
        s.search("nginx backups")
    except Exception as exc:
        errors += 1
        print("ERR", type(exc).__name__, exc, file=sys.stderr)
    finally:
        s.close()
sys.exit(1 if errors else 0)
"""


def _drive(client, jobs, errors):
    try:
        for tool, args in jobs:
            text = client.call(tool, **args)["content"][0]["text"]
            if "locked" in text or "busy" in text:
                errors.append(f"{tool}: {text[:200]}")
    except Exception as exc:  # pragma: no cover - reported through the assertion below
        errors.append(f"driver crashed: {type(exc).__name__}: {exc}")


def test_three_mcp_processes_plus_plugin_like_process_share_one_db(tmp_path, mcp_spawn):
    db = str(tmp_path / "shared.db")           # does not exist yet: all four start at the same moment
    env = {"HERMIND_DB": db}
    plugin = subprocess.Popen([sys.executable, "-I", "-c", PLUGIN_LIKE, str(ROOT), db, "8"],
                              stderr=subprocess.PIPE, text=True, env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    w1 = mcp_spawn("--write", "--agent", "w1", env=env, name="w1")
    w2 = mcp_spawn("--write", "--agent", "w2", env=env, name="w2")
    rd = mcp_spawn("--agent", "reader", env=env, name="rd")
    errors: list[str] = []
    n = 25
    jobs = {
        w1: [("memory_remember", {"content": f"writer one decision {i}: use postgres replica {i} for reporting",
                                  "keywords": "database replica reporting"}) for i in range(n)],
        w2: [("memory_remember", {"content": f"writer two decision {i}: rotate backup key {i} monthly",
                                  "keywords": "backup rotation"}) for i in range(n)],
        rd: [(t, a) for _ in range(n) for t, a in (("memory_search", {"query": "postgres replica"}),
                                                    ("memory_list", {"limit": 10}),
                                                    ("memory_search", {"query": "backup key"}))],
    }
    threads = [threading.Thread(target=_drive, args=(c, j, errors)) for c, j in jobs.items()]
    [t.start() for t in threads]
    [t.join(timeout=180) for t in threads]
    plugin_err = plugin.communicate(timeout=180)[1]
    stderrs = [c.stderr() for c in (w1, w2, rd)]
    for c in (w1, w2, rd):
        assert c.close() == 0
    assert not errors, errors[:5]
    assert plugin.returncode == 0, plugin_err
    assert "locked" not in plugin_err and not any("locked" in e for e in stderrs)
    import sqlite3
    con = sqlite3.connect(db)
    per_agent = dict(con.execute("SELECT source_session, COUNT(*) FROM notes WHERE source_session LIKE 'mcp:%' "
                                 "GROUP BY 1"))       # provenance is recorded as mcp:<agent>
    con.close()
    assert per_agent == {"mcp:w1": n, "mcp:w2": n}, per_agent
    checker = mcp_spawn(env=env, name="chk")
    got, _ = checker.call_json("memory_list", limit=50)
    assert got["count"] == 50
    one, _ = checker.call_json("memory_search", query="writer one decision 24 postgres replica 24", limit=3)
    assert one["count"] >= 1


import pytest


@pytest.mark.parametrize("round_", range(6))   # the race is timing dependent: several fresh databases
def test_simultaneous_first_start_on_a_fresh_database(tmp_path, mcp_spawn, round_):
    """Agents launched together by one editor all open a database that does not exist yet."""
    db = str(tmp_path / f"fresh{round_}.db")
    env = {"HERMIND_DB": db}
    clients = [mcp_spawn("--write", "--agent", f"a{i}", env=env, handshake=False, name=f"a{i}") for i in range(6)]
    results = {}

    def go(c):
        try:
            c.handshake()
            results[c] = c.call("memory_list")["isError"]
        except Exception as exc:
            results[c] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    errs = {i: c.stderr()[-400:] for i, c in enumerate(clients) if results.get(c) is not False}
    assert not errs, errs
