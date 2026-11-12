"""Load the plugin as package ``hermind`` without a Hermes runtime: stub ``agent.memory_provider`` and
``tools.registry`` when the real modules are not importable."""

import importlib.util
import json
import sys
import types
from abc import ABC, abstractmethod
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _stub_hermes():
    try:
        import agent.memory_provider  # noqa: F401
    except ImportError:
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider(ABC):
            @property
            @abstractmethod
            def name(self): ...

            @abstractmethod
            def is_available(self): ...

            @abstractmethod
            def initialize(self, session_id, **kwargs): ...

            @abstractmethod
            def get_tool_schemas(self): ...

        class RecallStatus:
            def __init__(self, provider_label, count, glyph="🧠"):
                self.provider_label, self.count, self.glyph = provider_label, count, glyph

        mp.MemoryProvider, mp.RecallStatus = MemoryProvider, RecallStatus
        mp.is_trivial_prompt = lambda t: not (t or "").strip() or (t or "").strip().lower() in {"ok", "thanks", "hi"}
        sys.modules.setdefault("agent", types.ModuleType("agent"))
        sys.modules["agent.memory_provider"] = mp
    try:
        import tools.registry  # noqa: F401
    except ImportError:
        reg = types.ModuleType("tools.registry")
        reg.tool_error = lambda m: json.dumps({"error": m})
        sys.modules.setdefault("tools", types.ModuleType("tools"))
        sys.modules["tools.registry"] = reg


def _load_package():
    mod = sys.modules.get("hermind")
    if mod is not None and getattr(mod, "__file__", None) == str(ROOT / "__init__.py") and getattr(mod, "_TEST_LOADED", False):
        return mod
    # The checkout folder is itself named "hermind", so pytest may already have imported it as a package before the
    # Hermes stubs existed. Drop that copy and load a fresh one after the stubs are in place.
    for name in [n for n in sys.modules if n == "hermind" or n.startswith("hermind.")]:
        del sys.modules[name]
    _stub_hermes()
    spec = importlib.util.spec_from_file_location("hermind", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermind"] = mod
    spec.loader.exec_module(mod)
    mod._TEST_LOADED = True
    return mod


@pytest.fixture(scope="session")
def pkg():
    return _load_package()


@pytest.fixture
def store_mod(pkg):
    return sys.modules["hermind.store"]


@pytest.fixture
def store(store_mod, tmp_path):
    s = store_mod.DecisionStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture
def provider(pkg, tmp_path):
    p = pkg.HermindProvider(config={"db_path": "$HERMES_HOME/dm.db"})
    p.initialize("20261018_120000_abcd", hermes_home=str(tmp_path), platform="telegram")
    yield p
    p.shutdown()


# -- MCP server helpers (tests/test_mcp_*.py) -----------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402

MCP_SCRIPT = ROOT / "hermind_mcp.py"


class MCPClient:
    """A real hermind_mcp.py subprocess spoken to over stdio. HOME points into tmp so nothing touches the user's
    files; stderr goes to a file (read with .stderr())."""

    def __init__(self, tmp_path, *args, env=None, name="c"):
        self.tmp = Path(tmp_path)
        self.err_path = self.tmp / f"{name}-{os.getpid()}-{id(self)}.err"
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("HERMIND_")}
        full_env.update({"HOME": str(self.tmp), "USERPROFILE": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"})
        full_env.update(env or {})
        self._err = open(self.err_path, "wb")
        self.proc = subprocess.Popen([sys.executable, "-I", str(MCP_SCRIPT), *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._err, env=full_env, cwd=str(self.tmp),
                                     text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self.raw_lines: list[str] = []

    def send_raw(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("server closed stdout")
        self.raw_lines.append(line)
        return json.loads(line)

    def request(self, method, params=None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))
        resp = self.read()
        assert resp["id"] == self._id
        return resp

    def notify(self, method, params=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))

    def handshake(self, version="2025-06-18", name="pytest-client") -> dict:
        resp = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                           "clientInfo": {"name": name, "version": "0"}})
        self.notify("notifications/initialized")
        return resp

    def call(self, tool, **arguments) -> dict:
        """The tools/call result dict (content/isError); raises AssertionError on a JSON-RPC error."""
        resp = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert "error" not in resp, resp
        return resp["result"]

    def call_json(self, tool, **arguments):
        res = self.call(tool, **arguments)
        return json.loads(res["content"][0]["text"]), res["isError"]

    def stderr(self) -> str:
        if not self._err.closed:
            self._err.flush()
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def close(self, timeout=10) -> int:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()
        finally:
            self._err.close()


@pytest.fixture
def mcp_spawn(tmp_path):
    made = []

    def spawn(*args, env=None, handshake=True, name="c"):
        c = MCPClient(tmp_path, *args, env=env, name=name)
        made.append(c)
        if handshake:
            c.handshake()
        return c

    yield spawn
    for c in made:
        if c.proc.poll() is None:
            c.close()
