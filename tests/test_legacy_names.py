import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HERMIND_VEC_HOME", raising=False)
    monkeypatch.delenv("DM_VEC_HOME", raising=False)


def test_vector_home_defaults_to_hermind_vec_but_keeps_an_existing_dm_vec(monkeypatch, tmp_path):
    import vec
    _home(monkeypatch, tmp_path)
    assert vec.default_vec_home() == str(tmp_path / ".hermes" / "hermind-vec")
    (tmp_path / ".hermes" / "dm-vec").mkdir(parents=True)
    assert vec.default_vec_home() == str(tmp_path / ".hermes" / "dm-vec")
    (tmp_path / ".hermes" / "hermind-vec").mkdir()
    assert vec.default_vec_home() == str(tmp_path / ".hermes" / "hermind-vec")
    monkeypatch.setenv("DM_VEC_HOME", "/x/old")
    assert vec.default_vec_home() == "/x/old"                      # the old variable still works
    monkeypatch.setenv("HERMIND_VEC_HOME", "/x/new")
    assert vec.default_vec_home() == "/x/new"                      # and the new one wins


def _run(script, *args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    for k in ("HERMIND_BACKFILL_BASE_URL", "HERMIND_BACKFILL_MODEL", "DM_BACKFILL_BASE_URL", "DM_BACKFILL_MODEL"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ROOT / "tools" / script), *args], capture_output=True, text=True, env=env)


def test_llm_helper_tools_do_not_default_to_a_private_endpoint_or_model():
    for script in ("build_summaries.py", "backfill_aliases.py"):
        r = _run(script, "--db", "nonexistent.db")
        assert r.returncode != 0 and "--base-url" in (r.stdout + r.stderr), (script, r.stdout, r.stderr)
        text = (ROOT / "tools" / script).read_text(encoding="utf-8")
        assert not re.search(r"(?:127\.0\.0\.1|localhost):\d+", text) and ":free" not in text     # no local default endpoint


def test_no_private_era_names_left_in_defaults():
    banned = ["backups\" / \"decision-memory", "~/.hermes/dm-vec\"))", "poolside/", "upstage/"]
    for f in list(ROOT.glob("*.py")) + list((ROOT / "tools").glob("*.py")):
        if f.name in ("vec.py", "backup.py"):      # these hold the documented fallback to an existing old folder
            continue
        text = f.read_text(encoding="utf-8")
        for b in banned:
            assert b not in text, (f.name, b)
