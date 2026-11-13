"""The 1.0.1 bug where `hermind review` did not find the MCP database shipped because CI only ran unit tests
against the checkout's own modules. This test builds and installs the hermind package the way a newcomer does (`uv
tool install`, or `pip install --target` when uv is not on PATH) and walks the README's MCP-only route against the
INSTALLED entry points, in a fresh, empty HOME. Skipped unless HERMIND_INSTALL_SMOKE=1, since it builds a package and
may need the network (uv fetching its own build dependencies) or write outside the normal test sandboxes; CI sets the
env var and runs only this file (see the install-smoke job in .github/workflows/test.yml)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMIND_INSTALL_SMOKE") != "1",
    reason="set HERMIND_INSTALL_SMOKE=1 to build and install the package like a newcomer (uv, or pip, offline)")

REPO = Path(__file__).resolve().parent.parent


def _clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMIND_", "HERMES_"))}
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Installs hermind in a scratch HOME and returns {"env", "hermind", "hermind_mcp", "how"}: argv prefixes for
    the two installed commands, an env dict to run them in, and which route was used (reported by the test)."""
    home = tmp_path_factory.mktemp("home")
    env = _clean_env(home)
    if shutil.which("uv"):
        work = tmp_path_factory.mktemp("uv")
        bin_dir = work / "bin"
        env.update(UV_TOOL_DIR=str(work / "tools"), UV_TOOL_BIN_DIR=str(bin_dir), UV_CACHE_DIR=str(work / "cache"),
                  UV_PYTHON_INSTALL_DIR=str(work / "python"))
        r = subprocess.run(["uv", "tool", "install", str(REPO)], capture_output=True, text=True, env=env, timeout=600)
        assert r.returncode == 0, "uv tool install failed:\n" + r.stdout + r.stderr
        exe = ".exe" if os.name == "nt" else ""
        return {"env": env, "hermind": [str(bin_dir / f"hermind{exe}")],
               "hermind_mcp": [str(bin_dir / f"hermind-mcp{exe}")], "how": "uv tool install"}
    venv_dir = tmp_path_factory.mktemp("venv")
    target = tmp_path_factory.mktemp("target")
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)], check=True, timeout=120)
    vpy = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    r = subprocess.run([str(vpy), "-m", "pip", "install", "--no-deps", "--no-build-isolation",
                       "--target", str(target), str(REPO)], capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, "pip install --target failed (no uv on PATH):\n" + r.stdout + r.stderr
    env["PYTHONPATH"] = str(target)
    bindir = target / ("Scripts" if os.name == "nt" else "bin")
    exe = ".exe" if os.name == "nt" else ""
    return {"env": env, "hermind": [str(vpy), str(bindir / f"hermind{exe}")],
           "hermind_mcp": [str(vpy), str(bindir / f"hermind-mcp{exe}")], "how": "pip install --target (no uv on PATH)"}


def _run(installed, *args, timeout=60):
    return subprocess.run([*installed["hermind"], *args], capture_output=True, text=True,
                          env=installed["env"], timeout=timeout)


def test_install_smoke(installed, capsys):
    print(f"\ninstall smoke: {installed['how']}")

    # 1. the installed entry point, not the checkout: no tools/deploy.sh sits next to it, and its package path is
    #    not inside this checkout at all.
    d = json.loads(_run(installed, "doctor", "--json").stdout)
    assert d["hermind"]["is_checkout"] is False
    assert not Path(d["hermind"]["path"]).resolve().is_relative_to(REPO.resolve())

    # 2. hermind selftest
    r = _run(installed, "selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "selftest passed" in r.stdout.strip().splitlines()[-1]

    # 3. remember, then search finds it
    note = "Install-smoke invented note: the lighthouse rotation schedule changed to every 90 seconds."
    r = _run(installed, "remember", note, "--keywords", "lighthouse rotation schedule")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _run(installed, "search", "lighthouse rotation")
    assert r.returncode == 0 and "lighthouse" in r.stdout, r.stdout + r.stderr

    # 4. the 1.0.1 regression: review with no --db and no HERMIND_DB, against the database remember just wrote
    r = _run(installed, "review")
    assert r.returncode == 0, r.stdout + r.stderr

    # 5. hermind-mcp over stdio: initialize, tools/list
    proc = subprocess.Popen(installed["hermind_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, env=installed["env"])
    try:
        def call(method, params=None, msg_id=1):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            assert line, "hermind-mcp closed stdout: " + proc.stderr.read()
            return json.loads(line)

        init = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "install-smoke", "version": "0"}})
        info = init["result"]["serverInfo"]
        assert info["name"] == "hermind" and info["version"]
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        tools = call("tools/list", msg_id=2)
        names = [t["name"] for t in tools["result"]["tools"]]
        assert "memory_search" in names
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            proc.wait()

    # 6. install-hermes --dry-run with no Hermes installed: SKIPPED, not FAILED
    r = _run(installed, "install-hermes", "--dry-run")
    assert r.stdout.startswith("SKIPPED"), r.stdout + r.stderr
