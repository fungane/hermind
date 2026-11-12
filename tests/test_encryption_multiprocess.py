"""The multi-process test again, on an ENCRYPTED database: MCP writers, a reader and a plugin-like process, all starting
at once on a database that does not exist yet. SQLCipher raises its own OperationalError class, so the lock-retry
paths are exercised for it here (the plain-sqlite version of this test cannot reach them)."""

import os
import subprocess
import sys
import threading

import pytest

pytest.importorskip("sqlcipher3")

sys.path.insert(0, os.path.dirname(__file__))
from test_mcp_multiprocess import PLUGIN_LIKE, ROOT, _drive  # noqa: E402

KEY = "multi-process key"


@pytest.mark.parametrize("round_", range(4))
def test_encrypted_shared_db_no_lock_errors(tmp_path, mcp_spawn, round_):
    db = str(tmp_path / f"enc{round_}.db")
    env = {"HERMIND_DB": db, "HERMIND_DB_KEY": KEY}
    plugin = subprocess.Popen([sys.executable, "-I", "-c", PLUGIN_LIKE, str(ROOT), db, "4"],
                              stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "HOME": str(tmp_path), "HERMIND_DB_KEY": KEY})
    w1 = mcp_spawn("--write", "--agent", "w1", env=env, name="w1")
    w2 = mcp_spawn("--write", "--agent", "w2", env=env, name="w2")
    rd = mcp_spawn("--agent", "reader", env=env, name="rd")
    errors: list[str] = []
    n = 15
    jobs = {
        w1: [("memory_remember", {"content": f"writer one decision {i}: use postgres replica {i} for reporting",
                                  "keywords": "database replica reporting"}) for i in range(n)],
        w2: [("memory_remember", {"content": f"writer two decision {i}: rotate backup key {i} monthly",
                                  "keywords": "backup rotation"}) for i in range(n)],
        rd: [(t, a) for _ in range(n) for t, a in (("memory_search", {"query": "postgres replica"}),
                                                    ("memory_list", {"limit": 10}))],
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
    assert "locked" not in plugin_err and not any("locked" in e for e in stderrs), (plugin_err, stderrs)
    assert open(db, "rb").read(15) != b"SQLite format 3"                     # created encrypted, never as plaintext
    sys.path.insert(0, str(ROOT))
    import dbdriver
    con = dbdriver.connect(db, KEY, use_env=False)
    per_agent = dict(con.execute("SELECT source_session, COUNT(*) FROM notes WHERE source_session LIKE 'mcp:%' GROUP BY 1"))
    con.close()
    assert per_agent == {"mcp:w1": n, "mcp:w2": n}, per_agent
