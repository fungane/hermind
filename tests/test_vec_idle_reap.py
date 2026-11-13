"""The semantic-search worker, once its own --idle timeout ends it, must not sit as a defunct (Z) child of the
parent until the next search. POSIX-only: the fake worker below is read with select() on a pipe, which Windows allows
only for sockets (see test_v04.py's vector tests for the same restriction)."""

import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"

# A stand-in for embed_server.py's own loop (see hermind/embed_server.py): reads one JSON line per request, answers
# with a fixed "ok" reply, and exits on its own after --idle seconds without a new request, exactly like the real worker.
FAKE_WORKER = '''
import select, sys
idle = float(sys.argv[sys.argv.index("--idle") + 1])
while True:
    r, _, _ = select.select([sys.stdin], [], [], idle)
    if not r:
        break
    line = sys.stdin.readline()
    if not line:
        break
    sys.stdout.write('{"ok": true, "dim": 1, "vecs": ""}\\n')
    sys.stdout.flush()
'''


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the fake worker is read with select() on a pipe, which Windows allows only for sockets")
def test_idle_worker_is_reaped_without_a_further_search(tmp_path):
    sys.path.insert(0, str(ROOT))
    from vec import VectorIndex
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    (tmp_path / "model.onnx").write_bytes(b"x")
    vi = VectorIndex({"home": str(tmp_path), "python": sys.executable, "server": str(worker),
                      "model": str(tmp_path / "model.onnx"), "tokenizer": str(tmp_path / "model.onnx"),
                      "model_name": "fake", "idle": 0.2}, str(tmp_path / "x.db"))
    try:
        resp = vi._call({"op": "ping"})
        assert resp and resp["ok"]
        pid = vi._proc.pid
        deadline = time.time() + vi.idle + 15    # the worker exits after idle; the parent's timer fires at idle + 2
        while vi._proc is not None and time.time() < deadline:
            time.sleep(0.1)
        assert vi._proc is None            # the parent noticed and reaped it without another _call()
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)    # already reaped: no defunct entry left behind
    finally:
        vi.close()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the fake worker is read with select() on a pipe, which Windows allows only for sockets")
def test_a_worker_still_in_use_when_the_timer_fires_is_left_alone(tmp_path):
    sys.path.insert(0, str(ROOT))
    from vec import VectorIndex
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    (tmp_path / "model.onnx").write_bytes(b"x")
    vi = VectorIndex({"home": str(tmp_path), "python": sys.executable, "server": str(worker),
                      "model": str(tmp_path / "model.onnx"), "tokenizer": str(tmp_path / "model.onnx"),
                      "model_name": "fake", "idle": 0.2}, str(tmp_path / "x.db"))
    try:
        assert vi._call({"op": "ping"}) and vi._proc is not None
        time.sleep(0.1)
        assert vi._call({"op": "ping"})["ok"]      # resets the worker's own idle clock before the first timer could fire
        assert vi._proc is not None and vi._proc.poll() is None
    finally:
        vi.close()
