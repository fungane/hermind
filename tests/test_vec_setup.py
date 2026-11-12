"""`hermind setup-vectors` and `hermind embed-all` (vec_setup.py). No network: the commands it would run are faked."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))
import vec_setup  # noqa: E402
from vec import PROFILES, VectorIndex  # noqa: E402


def test_every_profile_of_the_worker_has_a_model_pinned_to_a_revision_and_checksums():
    assert set(vec_setup.MODELS) == set(PROFILES)
    for name in PROFILES:
        p = vec_setup.plan(name, "/nowhere")
        assert p["pinned"] and p["revision"] and p["model"].endswith(PROFILES[name]["file"])
    with pytest.raises(ValueError):
        vec_setup.plan("huge", "/nowhere")


def _fake(tmp_path, calls, *, pip_ok=True):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        assert kw["env"]["ORT_DISABLE_TELEMETRY"] == "1" and kw["env"]["HF_HUB_DISABLE_TELEMETRY"] == "1"
        if cmd[1:4] == ["-m", "pip", "--version"]:
            return SimpleNamespace(returncode=0 if pip_ok else 1, stdout="", stderr="No module named pip")
        if "-c" in cmd:                       # the download step: put the two files where the worker looks
            out = Path(cmd[cmd.index("-c") + 2])
            assert cmd[-1] == str(tmp_path / "v" / ".download")
            for _, dst, _ in json.loads(cmd[-2]):
                (out / dst).write_bytes(b"x")
        if cmd[1:3] == ["-m", "venv"]:
            py = vec_setup.venv_python(tmp_path / "v")
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_run


def test_setup_runs_venv_pip_and_the_download_inside_the_folder_and_stops_at_the_first_failure(tmp_path):
    calls = []
    res = vec_setup.setup("light", str(tmp_path / "v"), run=_fake(tmp_path, calls), platform="linux")
    assert res["ok"] and res["model_exists"], res
    assert [c[1:3] for c in calls] == [["-m", "venv"], ["-m", "pip"], ["-c", vec_setup._FETCH]]
    assert all(str(tmp_path / "v") in " ".join(map(str, c)) for c in calls)
    assert (tmp_path / "v").stat().st_mode & 0o077 == 0 or sys.platform == "win32"

    calls.clear()                                      # second run: the venv is there (with pip), so it is not made again
    assert vec_setup.setup("light", str(tmp_path / "v"), run=_fake(tmp_path, calls), platform="linux")["ok"]
    assert [c[1:4] for c in calls] == [["-m", "pip", "--version"], ["-m", "pip", "install"], ["-c", vec_setup._FETCH, str(tmp_path / "v" / "model-light")]]

    calls.clear()                                      # a venv without pip is made again with --clear
    assert vec_setup.setup("light", str(tmp_path / "v"), run=_fake(tmp_path, calls, pip_ok=False), platform="linux")["ok"]
    assert calls[1][1:4] == ["-m", "venv", "--clear"]

    def failing(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: No matching distribution found for onnxruntime")
    bad = vec_setup.setup("balanced", str(tmp_path / "v"), run=failing, platform="linux")
    assert not bad["ok"] and "onnxruntime" in bad["error"]


def test_setup_refuses_on_windows_and_leaves_an_existing_folder_mode_alone(tmp_path):
    calls = []
    res = vec_setup.setup("light", str(tmp_path / "w"), run=_fake(tmp_path, calls), platform="win32")
    assert not res["ok"] and "Windows" in res["error"] and not calls and not (tmp_path / "w").exists()
    mine = tmp_path / "v"
    mine.mkdir()
    os.chmod(mine, 0o755)
    assert vec_setup.setup("light", str(mine), run=_fake(tmp_path, calls), platform="linux")["ok"]
    assert (mine.stat().st_mode & 0o777) == 0o755 or sys.platform == "win32"


def _fetch(tmp_path, files, rev="abc"):
    """Run the real _FETCH code with a fake huggingface_hub that hands over a read-only file, as the real cache does."""
    fake = tmp_path / "fakehub" / "huggingface_hub"
    fake.mkdir(parents=True, exist_ok=True)
    blob = tmp_path / "blob.onnx"
    if not blob.exists():
        blob.write_bytes(b"not the model")
        os.chmod(blob, 0o444)
    (fake / "__init__.py").write_text(f"def hf_hub_download(repo, src, revision=None, cache_dir=None):\n"
                                      f"    open({str(tmp_path / 'downloads.log')!r}, 'a').write(src + '\\n')\n"
                                      f"    return {str(blob)!r}\n")
    out, cache = tmp_path / "model", tmp_path / "cache"
    out.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, "-c", vec_setup._FETCH, str(out), "some/repo", rev, json.dumps(files), str(cache)],
                       capture_output=True, text=True, env={"PYTHONPATH": str(fake.parent)})
    return r, out, cache


def test_the_download_script_refuses_a_wrong_checksum_and_can_run_twice(tmp_path):
    import hashlib
    r, out, cache = _fetch(tmp_path, [["onnx/model.onnx", "model.onnx", "0" * 64]])
    assert r.returncode != 0 and "checksum mismatch" in r.stderr and not (out / "model.onnx").exists()
    assert not cache.exists()                                   # removed even when the run fails
    good = hashlib.sha256(b"not the model").hexdigest()
    for _ in range(2):                                           # the second run must not trip over a read-only file
        r, out, cache = _fetch(tmp_path, [["onnx/model.onnx", "model.onnx", None]])
        assert r.returncode == 0, r.stderr
        assert (out / "model.onnx").read_bytes() == b"not the model" and not cache.exists()
        assert os.access(out / "model.onnx", os.W_OK)
    (tmp_path / "downloads.log").unlink()
    r, out, _ = _fetch(tmp_path, [["onnx/model.onnx", "model.onnx", good]])
    assert r.returncode == 0 and not (tmp_path / "downloads.log").exists()   # right checksum in place: not fetched again


def test_the_worker_is_started_with_telemetry_off_and_its_cache_in_the_folder(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen.update(kw["env"])
            raise OSError("not really started")
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    vi = VectorIndex({"home": str(tmp_path / "v")}, str(tmp_path / "m.db"))
    with pytest.raises(OSError):
        vi._start()
    assert seen["ORT_DISABLE_TELEMETRY"] == "1" and seen["XDG_CACHE_HOME"] == str(tmp_path / "v" / ".cache")
    src = (ROOT / "embed_server.py").read_text(encoding="utf-8")
    assert src.index('os.environ["ORT_DISABLE_TELEMETRY"] = "1"') < src.index("import onnxruntime")


def test_embed_all_without_a_worker_says_how_to_set_it_up_and_picks_the_installed_profile(tmp_path):
    from store import DecisionStore
    db = tmp_path / "m.db"
    DecisionStore(str(db)).close()
    res = vec_setup.embed_all(str(db), {"profile": "light", "home": str(tmp_path / "none")})
    assert not res["ok"] and "hermind setup-vectors light" in res["error"]
    home = tmp_path / "v"
    vec_setup.venv_python(home).parent.mkdir(parents=True)
    vec_setup.venv_python(home).write_text("")
    for name in ("light", "balanced"):
        (home / PROFILES[name]["dir"]).mkdir()
        (home / PROFILES[name]["dir"] / PROFILES[name]["file"]).write_bytes(b"x")
        installed = vec_setup.installed_profiles(str(home))
        if len(installed) == 1:
            assert installed == ["light"]
    res = vec_setup.embed_all(str(db), {"home": str(home)})
    assert not res["ok"] and "several profiles" in res["error"] and "--profile" in res["error"]


def test_the_next_steps_name_a_folder_that_is_not_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMIND_VEC_HOME", raising=False)
    monkeypatch.delenv("DM_VEC_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    d = {"ok": True, "dry_run": True, **vec_setup.plan("light", "rel/vh")}
    assert d["home"] == str(tmp_path / "rel" / "vh")                     # made absolute
    text = vec_setup.describe(d)
    assert f"--home {tmp_path / 'rel' / 'vh'}" in text and f'"home": "{tmp_path / "rel" / "vh"}"' in text
    assert "--home" not in vec_setup.describe({"ok": True, **vec_setup.plan("light")})


def test_the_commands_answer_in_one_line(tmp_path):
    def cli(*args, script="hermind_cli.py"):
        return subprocess.run([sys.executable, str(ROOT / script), *args], capture_output=True, text=True)
    r = cli("setup-vectors", "light", "--dry-run", "--home", str(tmp_path / "v"), "--json")
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["dry_run"] and d["config"]["vectors"]["profile"] == "light"
    assert not (tmp_path / "v").exists()                       # a dry run changes nothing
    r = cli("light", "--dry-run", "--home", str(tmp_path / "v"), script="vec_setup.py")   # vec_setup.py run as a script
    assert r.returncode == 0 and r.stdout.startswith("would set up: profile light") and not (tmp_path / "v").exists()
    r = cli("embed-all", "--db", str(tmp_path / "missing.db"))
    assert r.returncode == 2 and r.stdout.startswith("ERROR: no database") and "Traceback" not in r.stderr
    (tmp_path / "junk.db").write_text("not a database")
    r = cli("embed-all", "--db", str(tmp_path / "junk.db"), "--home", str(tmp_path / "none"))
    assert r.returncode in (1, 2) and r.stdout.startswith("ERROR:") and "Traceback" not in r.stderr


def test_helper_scripts_answer_help_without_touching_anything(tmp_path):
    for script in ("export_hindsight.py",):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / script), "--help"], capture_output=True, text=True,
                           cwd=tmp_path)
        assert r.returncode == 0 and "usage" in r.stdout and not list(tmp_path.iterdir()), (script, r.stdout, r.stderr)
    assert not vec_setup.embed_all(str(tmp_path / "typo.db"), {"profile": "light", "home": str(tmp_path)})["ok"]
    assert not (tmp_path / "typo.db").exists()
