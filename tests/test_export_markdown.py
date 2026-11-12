"""SPEC-4 M3: `hermind export --format markdown --out DIR`: the folder layout, every option, a fixed clock."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_fixture import build_db, table_dump  # noqa: E402
from test_export_json import cli  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hermind_cli  # noqa: E402
import md_export  # noqa: E402

CLOCK = "2030-01-02 03:04:05"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "a.db"
    build_db(path)
    return path



@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(md_export, "export_time", lambda: CLOCK)
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def folder_bytes(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def by_command(db, out, *flags):
    assert hermind_cli.main(["export", "--db", str(db), "--format", "markdown", "--out", str(out), *flags]) == 0



@pytest.mark.parametrize("flags", [
    (),
    ("--include-history",),
    ("--by", "entity"),
    ("--by", "entity", "--include-history", "--scope", "work"),
    ("--scope", "default"),
])
def test_every_option_writes_a_folder_twice_the_same(tmp_path, db, flags):
    a, b = tmp_path / "first", tmp_path / "second"
    by_command(db, a, *flags)
    by_command(db, b, *flags)
    got = folder_bytes(a)
    assert got and got == folder_bytes(b)
    assert json.loads((a / ".hermind-export.json").read_text())["exported"] == CLOCK


def test_the_folder_has_the_layout_the_script_always_had(tmp_path, db):
    out = tmp_path / "o"
    by_command(db, out, "--include-history")
    files = {p.relative_to(out).as_posix() for p in out.rglob("*.md")}
    assert "index.md" in files and "tech/index.md" in files and "state/00005-The-Zephyr-gateway-listens-on-port-8080.md" in files
    assert (out / "state" / "00005-The-Zephyr-gateway-listens-on-port-8080.md").read_text().startswith("---\nid: 5\nkind: state\nstatus: superseded\n")
    plain = tmp_path / "p"
    by_command(db, plain)
    assert not any("00005-" in f for f in (p.name for p in plain.rglob("*.md")))   # without --include-history: no old versions


def test_a_folder_that_is_not_empty_is_refused_and_force_overwrites_only_same_named_files(tmp_path, db, capsys):
    out = tmp_path / "o"
    out.mkdir()
    (out / "mine.txt").write_text("keep me")
    r = cli(tmp_path, "export", "--db", str(db), "--format", "markdown", "--out", str(out))
    assert r.returncode == 2 and "is not empty" in r.stdout + r.stderr and "--force" in r.stdout + r.stderr
    assert [p.name for p in out.iterdir()] == ["mine.txt"]                          # nothing was written
    by_command(db, out, "--force")
    assert (out / "mine.txt").read_text() == "keep me" and (out / "index.md").exists()


def test_markdown_needs_a_folder_and_the_other_formats_refuse_the_wrong_options(tmp_path, db):
    for flags, words in ((("--format", "markdown"), "--out DIR"),
                         (("--format", "jsonl", "--live-only"), "--live-only"),
                         (("--format", "markdown", "--out", str(tmp_path / "x"), "--live-only"), "--live-only"),
                         (("--include-history",), "--include-history")):
        r = cli(tmp_path, "export", "--db", str(db), *flags)
        assert r.returncode == 2 and words in r.stdout + r.stderr, flags
    assert not (tmp_path / "x").exists()


def test_the_summary_line_and_the_json_result(tmp_path, db):
    out = tmp_path / "o"
    r = cli(tmp_path, "export", "--db", str(db), "--format", "markdown", "--out", str(out), "--include-history")
    assert r.returncode == 0
    assert r.stdout.strip() == f"exported 12 notes into 5 folder(s) under {out} (active 9, merged 1, superseded 2)"
    d = json.loads(cli(tmp_path, "export", "--db", str(db), "--format", "markdown", "--out", str(tmp_path / "o2"), "--json").stdout)
    assert d["ok"] and d["exported"] == 9 and d["folders"] == 5 and d["by_status"] == {"active": 9}


def test_export_only_reads(tmp_path, db):
    before = table_dump(db)
    by_command(db, tmp_path / "o", "--include-history", "--by", "entity")
    assert table_dump(db) == before


def test_the_folder_imports_back_as_the_round_trip_of_part_two_says(tmp_path, db):
    out = tmp_path / "o"
    by_command(db, out, "--include-history")
    b = tmp_path / "b.db"
    r = cli(tmp_path, "import", str(out), "--db", str(b), "--apply", "--json", "--no-backup")
    d = json.loads(r.stdout)
    assert d["ok"], d
    assert d["counts"]["new"] == 9 and d["counts"]["history"] == 3        # live notes come in; old versions stay history
    from store import DecisionStore
    s = DecisionStore(str(b))
    try:
        rows = {r["content"]: r for r in s.conn.execute("SELECT * FROM notes")}
    finally:
        s.close()
    assert len(rows) == 9 and all(r["status"] == "active" for r in rows.values())
    pinned = rows["Always answer in British English."]
    rich = rows["Helios audit reports are kept for seven years."]
    assert pinned["pinned"] == 1 and rich["trust"] == 0.9 and "audit" in rich["tags"] and rich["scope"] == "work"
    assert "Priya keeps the Orchid dashboard on SQLite since March." in rows          # the current text, not the old one
    assert "Priya keeps the Orchid dashboard on PostgreSQL." not in rows


def test_the_markdown_folder_is_readable_by_the_owner_only(tmp_path):
    """Notes can be sensitive: like the JSON export (mode 600), every file is 600 and every folder 700."""
    import os
    import stat
    import store as store_mod
    db = tmp_path / "p.db"
    s = store_mod.DecisionStore(str(db))
    s.add("Ines keeps the spare key to the allotment shed.", kind="state")
    s.add("The allotment committee meets on the first Monday.", kind="decision")
    s.close()
    out = tmp_path / "out"
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, os.path.join(root, "hermind_cli.py"), "export", "--format", "markdown",
                    "--db", str(db), "--out", str(out)], check=True, capture_output=True)
    paths = [out, *out.rglob("*")]
    assert len(paths) > 3
    for p in paths:
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))
