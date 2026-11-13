"""Set up the optional semantic-search worker, and embed the notes that have no vector yet.

`hermind setup-vectors PROFILE` makes a private virtualenv (onnxruntime, tokenizers, numpy, huggingface_hub) and puts
one local ONNX embedding model next to it, all inside one folder: $HERMIND_VEC_HOME, else ~/.hermes/hermind-vec (an
existing ~/.hermes/dm-vec is kept). Nothing outside that folder is written (pip and the model download keep no cache
elsewhere), and deleting it removes everything.
`hermind embed-all --db FILE` then gives every existing note a vector; new notes are embedded in the background.

The models are pinned to a revision and checked against a checksum: a file that differs is not installed. The worker's
four Python packages are the newest PyPI has for this Python (they are not pinned).
Telemetry: Hugging Face's is switched off for the download, and onnxruntime's (on by default in its official builds) is
switched off for the worker (vec.py and embed_server.py set ORT_DISABLE_TELEMETRY=1).

Standard library only; lives in the package so both commands work from a pip or uvx install, not only from a checkout.
Also runnable as a script (`python3 vec_setup.py PROFILE [--home DIR] [--dry-run]`).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from .vec import PROFILES, VectorIndex, default_vec_home
except ImportError:  # loaded as a top-level module (hermind_cli.py from a checkout, tests, tools/, run as a script)
    from vec import PROFILES, VectorIndex, default_vec_home

DEPS = ("onnxruntime", "tokenizers", "numpy", "huggingface_hub")

# profile -> (Hugging Face repo, revision, ((file in the repo, file name here, sha256), ...), MB to download, RAM)
MODELS = {
    "default": ("Xenova/multilingual-e5-small", "761b726dd34fb83930e26aab4e9ac3899aa1fa78",
                (("onnx/model_quantized.onnx", "model_quantized.onnx", "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
                 ("tokenizer.json", "tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39")),
                118, "about 495 MB"),
    "light": ("hotchpotch/bekko-static-embedding-v1", "c18167b8f244c1bc6de21ce9b710bac8a29c4615",
              (("onnx/model_int8_dim256.onnx", "model.onnx", "b79091ffcc23d499ea0dcc52d6d08e4aecccd1ca8b866d5c02e1f5c082d23158"),
               ("tokenizer.json", "tokenizer.json", "308823060352e35bf9fff6b42ef343f31cc68fa800e284568cc44e9cc0f70a73")),
              52, "about 180 MB"),
    "balanced": ("hindsight-ai/hs-embed-1", "3164caa4db95a03faac8a327f914f7c8f42d2f98",
                 (("onnx/model.onnx", "model.onnx", "ad880bb19889113774d139ca02c20d4eeda936ebefb3e0d647492b20968ce432"),
                  ("tokenizer.json", "tokenizer.json", "f34b44a6dc8a0eef651bfa10e8325057530152606a69d9c3b6ae02efb1558f0a")),
                 152, "about 610 MB"),
    "quality": ("onnx-community/bge-m3-ONNX", "25b9af8e87a38eb120cfe87125383677b9cd309e",
                (("onnx/model_int8.onnx", "model.onnx", "2237f770aad5c71bbc1fc2d361a57f9a37400574cc9eff32626f0cdb49234730"),
                 ("tokenizer.json", "tokenizer.json", "249df0778f236f6ece390de0de746838ef25b9d6954b68c2ee71249e0a9d8fd4")),
                568, "about 960 MB"),
}

# Runs inside the private venv (it needs huggingface_hub): download, check, copy. Arguments: target folder, repo, revision,
# JSON files, download cache. A file already in place with the right checksum is kept. Each file is copied under a
# temporary name, made writable by its owner (the download cache hands out read-only files) and moved into place, so a
# second run can replace it. The cache is a folder of its own inside the setup folder and is removed in any case, so no
# second copy of the model stays in ~/.cache.
_FETCH = r"""
import hashlib, json, os, shutil, sys
from huggingface_hub import hf_hub_download
out, repo, rev, files, cache = sys.argv[1], sys.argv[2], sys.argv[3] or None, json.loads(sys.argv[4]), sys.argv[5]

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

try:
    for src, dst, want in files:
        final = os.path.join(out, dst)
        if want and os.path.exists(final) and sha(final) == want:
            continue
        path = hf_hub_download(repo, src, revision=rev, cache_dir=cache)
        if want and sha(path) != want:
            sys.exit(f"checksum mismatch for {src}: not installing")
        tmp = final + ".part"
        shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
finally:
    shutil.rmtree(cache, ignore_errors=True)
"""


def venv_python(home: Path) -> Path:
    return home / "venv" / "bin" / "python"        # POSIX only: setup refuses on Windows (vec.py starts this path)


def resolve_home(home: str | None) -> Path:
    return Path(os.path.abspath(os.path.expanduser(home or default_vec_home())))


def plan(profile: str, home: str | None = None) -> dict:
    """What `setup-vectors` would do, without doing it. Raises ValueError for an unknown profile."""
    profile = (profile or "default").strip().lower()
    if profile not in MODELS:
        raise ValueError(f"unknown profile {profile!r}: choose one of {', '.join(MODELS)}")
    base = resolve_home(home)
    repo, rev, files, mb, ram = MODELS[profile]
    prof = PROFILES[profile]
    model_dir = base / prof["dir"]
    vectors = {"enabled": True, "profile": profile}
    if home or os.environ.get("HERMIND_VEC_HOME") or os.environ.get("DM_VEC_HOME"):
        vectors["home"] = str(base)                  # not the default folder: the server must be told where it is
    return {"profile": profile, "home": str(base), "venv": str(base / "venv"), "venv_exists": venv_python(base).exists(),
            "model_dir": str(model_dir), "model": str(model_dir / prof["file"]),
            "model_exists": (model_dir / prof["file"]).exists() and (model_dir / "tokenizer.json").exists(),
            "repo": repo, "revision": rev, "pinned": rev is not None and all(w for _, _, w in files),
            "download_mb": mb, "ram_while_awake": ram, "packages": list(DEPS), "config": {"vectors": vectors}}


def installed_profiles(home: str | None = None) -> list[str]:
    """The profiles whose model files are in the folder (and whose venv exists)."""
    base = resolve_home(home)
    if not venv_python(base).exists():
        return []
    return [n for n, p in PROFILES.items() if (base / p["dir"] / p["file"]).exists()]


def setup(profile: str, home: str | None = None, *, run=subprocess.run, log=None, platform: str = sys.platform) -> dict:
    """Make the venv (if missing or without pip), install the worker's packages and fetch the model. Returns the plan
    plus ``ok`` and, on failure, ``error`` (one line; nothing outside the folder was touched)."""
    p = plan(profile, home)
    if platform == "win32":
        return {**p, "ok": False, "error": "semantic search is not supported on Windows yet (see docs/limitations.md)"}
    say = log or (lambda msg: None)
    base, model_dir = Path(p["home"]), Path(p["model_dir"])
    repo, rev, files, _, _ = MODELS[p["profile"]]
    try:
        if not base.exists():
            base.mkdir(parents=True)
            os.chmod(base, 0o700)                   # a folder made here is the owner's only; an existing one is left as it is
        model_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {**p, "ok": False, "error": f"cannot create {base}: {exc}"}
    env = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "ORT_DISABLE_TELEMETRY": "1",
           "HF_HOME": str(base / ".download"), "XDG_CACHE_HOME": str(base / ".cache")}
    py = venv_python(base)
    steps = []
    if not py.exists():
        steps.append(("creating the private virtualenv", [sys.executable, "-m", "venv", str(base / "venv")]))
    else:
        try:
            has_pip = run([str(py), "-m", "pip", "--version"], capture_output=True, text=True, env=env).returncode == 0
        except OSError:
            has_pip = False
        if not has_pip:                              # a venv left without pip (a failed ensurepip) is made again
            steps.append(("re-creating the private virtualenv (it had no pip)",
                          [sys.executable, "-m", "venv", "--clear", str(base / "venv")]))
    steps.append((f"installing {', '.join(DEPS)} into it", [str(py), "-m", "pip", "install", "-q", "--no-cache-dir", *DEPS]))
    steps.append((f"downloading {repo} (about {p['download_mb']} MB)",
                  [str(py), "-c", _FETCH, str(model_dir), repo, rev or "", json.dumps(files), str(base / ".download")]))
    for what, cmd in steps:
        say(what)
        try:
            r = run(cmd, capture_output=True, text=True, env=env)
        except OSError as exc:
            return {**p, "ok": False, "error": f"{what} failed: {exc}"}
        if r.returncode != 0:
            tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
            text = " ".join(tail)
            hint = (" (on Debian/Ubuntu: apt install python3-venv)"
                    if "virtualenv" in what and ("ensurepip" in text or "venv" in text) else "")
            return {**p, "ok": False, "error": f"{what} failed{hint}: {tail[-1] if tail else 'exit ' + str(r.returncode)}"}
    p = plan(p["profile"], home)
    if not p["model_exists"]:
        return {**p, "ok": False, "error": f"the model files are not in {p['model_dir']} after the download"}
    return {**p, "ok": True}


def describe(d: dict, cli: str = "hermind") -> str:
    """The text answer of setup-vectors and embed-all."""
    if not d["ok"]:
        return "ERROR: " + d["error"]
    if "embedded" in d:
        lines = [f"done: {d['embedded']} notes embedded with {d['model']} in {d['seconds']}s"]
        lines += [f"note: {n}" for n in d.get("notes", [])]
        return "\n".join(lines)
    home = f" --home {d['home']}" if "home" in d["config"]["vectors"] else ""
    lines = [f"{'would set up' if d.get('dry_run') else 'ready'}: profile {d['profile']} ({d['repo']}, about "
             f"{d['download_mb']} MB, RAM while awake {d['ram_while_awake']})", f"  folder: {d['home']}"]
    if not d["pinned"]:
        lines.append("  this model is not pinned to a checksum")
    lines += ["next:", f"  embed your existing notes:   {cli} embed-all --db FILE --profile {d['profile']}{home}",
              f"  MCP server:   hermind-mcp --config FILE.json   with FILE.json = {json.dumps(d['config'])}",
              "  Hermes plugin:   hermes config set plugins.hermind.vectors.enabled true && "
              f"hermes config set plugins.hermind.vectors.profile {d['profile']}"
              + (f" && hermes config set plugins.hermind.vectors.home {d['home']}" if home else "")]
    return "\n".join(lines)


def embed_all(db: str, cfg: dict | None = None, *, batch: int = 64, log=None) -> dict:
    """Give every note of *db* that has no vector for the configured model one. Without a profile in *cfg*, the one
    profile installed is used (several installed: ``ok`` False, pick one). Returns counts; ``ok`` is False when the
    worker is not set up or stopped answering."""
    try:
        from .store import DecisionStore
        from .store_common import with_legacy_fallback
        from . import dbdriver
    except ImportError:
        from store import DecisionStore
        from store_common import with_legacy_fallback
        import dbdriver
    cfg = dict(cfg or {})
    db = str(with_legacy_fallback(db))
    if not os.path.isfile(db):                       # never create an empty database by embedding into a typo
        return {"ok": False, "embedded": 0, "error": f"no database at {db}"}
    if not cfg.get("profile"):
        have = installed_profiles(cfg.get("home"))
        if len(have) > 1:
            return {"ok": False, "embedded": 0, "error": f"several profiles are installed ({', '.join(have)}): pass --profile; "
                                                          "each note keeps one vector, so embedding with another profile replaces them"}
        cfg["profile"] = have[0] if have else "default"
    vi = VectorIndex(cfg, db)
    if not vi.available():
        return {"ok": False, "error": "the embedding worker is not set up: run `hermind setup-vectors "
                                      f"{cfg['profile']}` first", "embedded": 0}
    notes = []
    if dbdriver.is_plaintext_file(db) is False:     # encrypted: the worker opens the file itself to search it
        check = subprocess.run([vi.python, "-c", "import sqlcipher3"], capture_output=True)
        if check.returncode != 0:
            notes.append(f"the database is encrypted and the worker's venv has no sqlcipher3, so it can embed but not search: "
                         f"{Path(vi.python).parent / 'pip'} install sqlcipher3")
    store = DecisionStore(db, track_access=False)
    t, total = time.time(), 0
    try:
        while True:
            n = vi.index_missing(store, batch)
            if not n:
                break
            total += n
            if log:
                log(f"{total} notes embedded, {time.time() - t:.0f}s")
        left = len(store.missing_vectors(vi.model_name, 1))
    finally:
        vi.close()
        store.close()
    out = {"ok": not left, "embedded": total, "seconds": round(time.time() - t, 1), "model": vi.model_name,
           "profile": cfg["profile"], "db": db, "notes": notes}
    if left:
        out["error"] = (f"some notes still have no vector for '{vi.model_name}': the embedding worker did not answer "
                        f"(see {db}.vec.log)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vec_setup.py", description="set up the optional semantic-search worker "
                                                                  "(the same as `hermind setup-vectors`)")
    ap.add_argument("profile", nargs="?", default="default", choices=tuple(MODELS))
    ap.add_argument("--home")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    if ns.dry_run:
        d = {"ok": True, "dry_run": True, **plan(ns.profile, ns.home)}
    else:
        d = setup(ns.profile, ns.home, log=lambda m: print(f"... {m}", file=sys.stderr))
    print(describe(d, cli="python3 hermind_cli.py"))
    return 0 if d["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
