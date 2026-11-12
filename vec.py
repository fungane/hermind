"""Client for embed_server.py: lazily starts the worker process, indexes notes that have no vector yet and answers
semantic searches. Every failure returns None / 0 and trips a circuit breaker (callers then use lexical search only)."""
from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path


# Ready-made model settings (sizes and memory measured on a 4-core server, model loaded and answering queries):
#   default   multilingual-e5-small int8   118 MB file, about 480 MB RAM  (the original; keeps existing vectors)
#   light     bekko-static-embedding-v1    52 MB file, about 160 MB RAM, embeds thousands of notes in seconds
#   balanced  hs-embed-1 (Hindsight, MIT)   152 MB file, about 600 MB RAM, better Chinese/English recall than default
#   quality   bge-m3 int8                   568 MB file, about 910 MB RAM, best recall in our tests, slow to index
# Each note keeps ONE vector (the table key is note_id): switching profile makes the old vectors unusable and the new
# model replaces them as it embeds, so going back means embedding everything again. Back up the database first.
PROFILES = {
    "default": {"dir": "model", "file": "model_quantized.onnx", "model_name": "e5", "query_prefix": "query: ", "passage_prefix": "passage: ", "pooling": "mean", "sec_per_note": 0.15},
    "light": {"dir": "model-light", "file": "model.onnx", "model_name": "bekko-static-256", "query_prefix": "", "passage_prefix": "", "pooling": "mean", "sec_per_note": 0.05},
    "balanced": {"dir": "model-balanced", "file": "model.onnx", "model_name": "hs-embed-1", "query_prefix": "", "passage_prefix": "", "pooling": "mean", "sec_per_note": 0.15},
    "quality": {"dir": "model-quality", "file": "model.onnx", "model_name": "bge-m3", "query_prefix": "", "passage_prefix": "", "pooling": "cls", "sec_per_note": 0.8},
}


def default_vec_home() -> str:
    """$HERMIND_VEC_HOME (or the old $DM_VEC_HOME), else ~/.hermes/hermind-vec, unless an older ~/.hermes/dm-vec is what is installed."""
    env = os.environ.get("HERMIND_VEC_HOME") or os.environ.get("DM_VEC_HOME")
    if env:
        return env
    old, new = Path("~/.hermes/dm-vec").expanduser(), Path("~/.hermes/hermind-vec").expanduser()
    return str(old if old.exists() and not new.exists() else new)


class VectorIndex:
    def __init__(self, cfg: dict, db_path: str):
        base = Path(os.path.expanduser(str(cfg.get("home") or default_vec_home())))
        self.home = base
        prof = PROFILES.get(str(cfg.get("profile") or "default").strip().lower(), PROFILES["default"])
        self.python = os.path.expanduser(str(cfg.get("python") or base / "venv" / "bin" / "python"))
        self.server = str(cfg.get("server") or Path(__file__).resolve().parent / "embed_server.py")
        self.model = os.path.expanduser(str(cfg.get("model") or base / prof["dir"] / prof["file"]))
        self.tokenizer = os.path.expanduser(str(cfg.get("tokenizer") or base / prof["dir"] / "tokenizer.json"))
        self.model_name = str(cfg.get("model_name") or prof["model_name"])
        # e5 models need "query: " / "passage: " in front of the text; other models must get the text as it is
        self.query_prefix = str(cfg["query_prefix"]) if "query_prefix" in cfg else prof["query_prefix"]
        self.passage_prefix = str(cfg["passage_prefix"]) if "passage_prefix" in cfg else prof["passage_prefix"]
        self.pooling = str(cfg.get("pooling") or prof["pooling"]).lower()
        # how long to wait per note when embedding a batch (bge-m3 needs about 0.3 s per note on 2 cores; the margin is on purpose)
        self.sec_per_note = float(cfg.get("sec_per_note") or prof["sec_per_note"])
        self.idle = float(cfg.get("idle") or 300)
        self.timeout = float(cfg.get("timeout") or 30)
        self.k = max(5, min(60, int(cfg.get("candidates") or 30)))
        self.db_path = db_path
        self._proc = None
        self._lock = threading.Lock()
        self._fails = 0
        self._skip_until = 0.0

    # -- process ------------------------------------------------------------------------------------------------
    def available(self) -> bool:
        return time.time() >= self._skip_until and os.path.exists(self.python) and os.path.exists(self.model)

    def _start(self):
        # the worker's last error goes to <db>.vec.log (owner-only, overwritten at each start), so a worker that cannot start,
        # e.g. no sqlcipher3 in its venv for an encrypted database, is not silent
        try:
            log = open(self.db_path + ".vec.log", "w", encoding="utf-8")
            os.chmod(self.db_path + ".vec.log", 0o600)
        except OSError:
            log = subprocess.DEVNULL
        self._proc = subprocess.Popen(
            [self.python, self.server, "--model", self.model, "--tokenizer", self.tokenizer, "--db", self.db_path,
             "--model-name", self.model_name, "--idle", str(self.idle), "--pooling", self.pooling],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
            # onnxruntime sends telemetry to Microsoft by default: off (embed_server.py sets it too). It also keeps files
            # under the user's cache folder; any it still writes go to this worker's own folder instead
            env={**os.environ, "ORT_DISABLE_TELEMETRY": "1", "XDG_CACHE_HOME": str(self.home / ".cache")})
        if log is not subprocess.DEVNULL:
            log.close()

    def _call(self, req: dict, timeout: float | None = None) -> dict | None:
        if not self.available():
            return None
        timeout = timeout or self.timeout
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
                if not ready:
                    raise TimeoutError("embedding worker did not answer")
                line = self._proc.stdout.readline()
                resp = json.loads(line) if line else None
                if not resp or not resp.get("ok"):
                    raise RuntimeError((resp or {}).get("error", "worker exited"))
                self._fails = 0
                return resp
            except Exception:
                self._fails += 1
                self.close()
                if self._fails >= 3:
                    self._skip_until = time.time() + 300
                    self._fails = 0
                return None

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass

    # -- API ----------------------------------------------------------------------------------------------------
    def index_missing(self, store, limit: int = 32) -> int:
        """Embed up to *limit* notes that have no vector yet (first load of the model takes ~2 s)."""
        todo = store.missing_vectors(self.model_name, limit)
        if not todo:
            return 0
        resp = self._call({"op": "embed", "prefix": self.passage_prefix, "texts": [t for _, t in todo]},
                          timeout=max(self.timeout, 10 + self.sec_per_note * len(todo)))
        if not resp:
            return 0
        dim = int(resp["dim"])
        raw = base64.b64decode(resp["vecs"])
        step = dim * 4
        store.put_vectors(self.model_name, [(nid, raw[i * step:(i + 1) * step]) for i, (nid, _) in enumerate(todo)])
        return len(todo)

    def search(self, query: str, timeout: float | None = None) -> list[tuple[int, float]] | None:
        resp = self._call({"op": "search", "query": query, "k": self.k, "prefix": self.query_prefix}, timeout=timeout)
        return [(int(i), float(s)) for i, s in resp["hits"]] if resp else None
