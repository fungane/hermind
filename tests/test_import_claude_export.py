"""The `claude-export` reader of `hermind import` (synthetic exports: the real format is not published)."""

import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
import reader_exports as rx  # noqa: E402
from store import DecisionStore  # noqa: E402

MEMORY = """**Work context**
Ravi is a hydrologist at the Lakeview water board.

**Personal context**
Ravi keeps bees and is learning Portuguese.

**Top of mind**
He is preparing a flood-risk talk for October.

**Brief history**

*Recent months*

Ravi asked for help cleaning river-gauge data.

*Earlier context*

He moved from Pune two years ago.
"""
U1, U2, U3 = "11111111-aaaa-4aaa-8aaa-000000000001", "22222222-bbbb-4bbb-8bbb-000000000002", "33333333-cccc-4ccc-8ccc-000000000003"
PROJECT_MEMORY = {U1: "# Gauges\nThe Lakeview gauges report every 15 minutes.\n\n# Units\nAll levels are in centimetres.",
                  "99999999-dddd-4ddd-8ddd-000000000009": "An unknown project keeps its notes in a shared drive."}


def project(uuid, name, **kw):
    return {"uuid": uuid, "name": name, "description": "", "prompt_template": "", "is_starter_project": False, "docs": [],
            "creator": {"uuid": "x"}, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z", **kw}


PROJECTS = [project(U1, "River gauges", description="Cleaning gauge data for the water board.",
                    prompt_template="# Style\nAnswer with units.\n\n# Format\nUse metric only.",
                    docs=[{"filename": "readings.csv", "content": "a,b\n1,2\n"}, {"filename": "notes.txt", "content": "hello"}]),
            project(U2, "Bees", description="", prompt_template="Keep answers short and practical."),
            project(U3, "Getting started", is_starter_project=True, description="How to use Claude",
                    prompt_template="Welcome!")]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def files(split=False):
    f = {"memories.json": json.dumps([{"conversations_memory": MEMORY, "project_memories": PROJECT_MEMORY, "account_uuid": "acc"}]),
         "conversations.json": json.dumps([{"uuid": "c1", "chat_messages": [{"text": "secret chat text"}]}]),
         "users.json": json.dumps([{"uuid": "acc", "full_name": "Private Person"}])}
    if split:
        for p in PROJECTS:
            f[f"projects/{p['uuid']}.json"] = json.dumps(p)
    else:
        f["projects.json"] = json.dumps(PROJECTS)
    return f


def folder(root: Path, **kw):
    for name, text in files(**kw).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def run(src, db, **kw):
    return import_core.run_import(str(src), db, **kw)


def view(out):
    return [(e["origin"], e["outcome"], e.get("text", ""), e.get("kind", "")) for e in out["entries"]]


def stored(db):
    s = DecisionStore(str(db))
    try:
        return [r["content"] for r in s.conn.execute("SELECT content FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


def test_the_memory_text_gives_the_five_blocks_and_none_is_only_a_heading(tmp_path):
    f = tmp_path / "x"
    f.mkdir()
    (f / "memories.json").write_text(json.dumps([{"conversations_memory": MEMORY}]), encoding="utf-8")
    out = run(f, tmp_path / "c.db", apply=True, backup=False)
    assert out["reader"] == "claude-export" and out["reader_detected"] and out["counts"]["new"] == 5
    assert [e["origin"] for e in out["entries"]] == [f"memory#{i}" for i in range(1, 6)]
    assert all(e["kind"] == "preference" for e in out["entries"])
    assert stored(tmp_path / "c.db") == [
        "Work context\n\nRavi is a hydrologist at the Lakeview water board.",
        "Personal context\n\nRavi keeps bees and is learning Portuguese.",
        "Top of mind\n\nHe is preparing a flood-risk talk for October.",
        "Brief history › Recent months\n\nRavi asked for help cleaning river-gauge data.",
        "Brief history › Earlier context\n\nHe moved from Pune two years ago."]
    assert not any(t.strip().startswith("**") or "\n\n" not in t for t in stored(tmp_path / "c.db"))


def test_one_record_instead_of_a_list_works_too(tmp_path):
    f = tmp_path / "x"
    f.mkdir()
    (f / "memories.json").write_text(json.dumps({"conversations_memory": "Ravi likes tea."}), encoding="utf-8")
    assert run(f, tmp_path / "c.db")["counts"]["new"] == 1


def test_project_memories_for_a_known_and_an_unknown_uuid_with_their_labels(tmp_path):
    out = run(folder(tmp_path / "x"), tmp_path / "c.db", apply=True, backup=False)
    origins = [e["origin"] for e in out["entries"] if e["origin"].startswith("project-memory/")]
    assert origins == ["project-memory/River gauges#1", "project-memory/River gauges#2",
                       "project-memory/99999999-dddd-4ddd-8ddd-000000000009#1"]
    texts = stored(tmp_path / "c.db")
    assert "River gauges › Units\n\nAll levels are in centimetres." in texts
    assert "99999999-dddd-4ddd-8ddd-000000000009\n\nAn unknown project keeps its notes in a shared drive." in texts


def test_projects_json_and_one_file_per_project_give_the_same_entries(tmp_path):
    a = run(folder(tmp_path / "a"), tmp_path / "a.db")
    b = run(folder(tmp_path / "b", split=True), tmp_path / "b.db")
    assert view(a) == view(b) and a["groups"] == b["groups"]
    origins = [e["origin"] for e in a["entries"]]
    assert "project-instructions/River gauges#1" in origins and "project-instructions/Bees#1" in origins
    assert "project-description/River gauges" in origins and "project-description/Bees" not in origins   # empty description
    assert not any("Getting started" in o for o in origins)                                              # starter project
    joined = "\n".join(a["remarks"])
    assert "1 starter project was skipped" in joined and "Getting started" in joined
    assert "2 project documents were not imported" in joined and "readings.csv" in joined and "notes.txt" in joined
    assert "(13 bytes in all;" in joined
    kinds = {e["origin"]: e["kind"] for e in a["entries"]}
    assert kinds["memory#1"] == "preference"
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name, text in files().items():
            zf.writestr(name, text)
    assert view(run(z, tmp_path / "z.db")) == view(a)


def test_users_json_and_conversations_json_with_mode_000_cause_no_error(tmp_path):
    f = folder(tmp_path / "x")
    for n in ("users.json", "conversations.json"):
        (f / n).chmod(0)
    try:
        out = run(f, tmp_path / "c.db", apply=True, backup=False)
    finally:
        for n in ("users.json", "conversations.json"):
            (f / n).chmod(0o644)
    assert out["ok"] and out["reader"] == "claude-export"
    joined = "\n".join(out["remarks"])
    assert "conversations.json (" in joined and "was not read: conversations are not memory" in joined
    assert "users.json (never opened)" in joined
    assert not any("secret chat" in t or "Private Person" in t for t in stored(tmp_path / "c.db"))


def test_conversations_json_is_only_looked_at_by_detection(tmp_path):
    f = tmp_path / "x"
    f.mkdir()
    (f / "conversations.json").write_text('[{"uuid": "c", "chat_messages": []}]', encoding="utf-8")
    assert import_core.detect_reader(f) == ("claude-export", [])
    opened = []
    real = rx.ExportSource.open

    def spy(self, name):
        opened.append(name)
        return real(self, name)

    rx.ExportSource.open = spy
    try:
        out = run(f, tmp_path / "c.db", reader="claude-export")
    finally:
        rx.ExportSource.open = real
    assert out["total"] == 0 and opened == [] and any("no memories.json" in r for r in out["remarks"])


def test_group_exclusion_and_apply_twice(tmp_path):
    f = folder(tmp_path / "x")
    base = run(f, tmp_path / "c.db")
    assert [g["name"] for g in base["groups"]] == ["memory", "project memory", "project instructions", "project descriptions"]
    assert [g["exclude"] for g in base["groups"]] == ["memory#*", "project-memory/*", "project-instructions/*", "project-description/*"]
    for k in import_core.OUTCOMES:
        assert sum(g["counts"][k] for g in base["groups"]) == base["counts"][k]
    for g in base["groups"]:
        out = run(f, tmp_path / "c.db", exclude=[g["exclude"]])
        for e0, e1 in zip(base["entries"], out["entries"]):
            if e0["group"] == g["name"]:
                assert e1["outcome"] == "excluded"
            else:
                assert (e1["outcome"], e1["reason"]) == (e0["outcome"], e0["reason"])
    first = run(f, tmp_path / "c.db", apply=True, backup=False)
    second = run(f, tmp_path / "c.db", apply=True, backup=False)
    assert second["counts"]["new"] == 0 and second["counts"]["duplicate"] == first["counts"]["new"]


def test_two_projects_with_one_name_fall_back_to_their_uuid(tmp_path):
    f = tmp_path / "x"
    f.mkdir()
    (f / "projects.json").write_text(json.dumps([project(U1, "Same", prompt_template="First rule."),
                                                  project(U2, "Same", prompt_template="Second rule.")]), encoding="utf-8")
    out = run(f, tmp_path / "c.db", reader="claude-export")
    assert [e["origin"] for e in out["entries"]] == [f"project-instructions/{U1}#1", f"project-instructions/{U2}#1"]


def test_unreadable_json_is_a_remark_not_a_crash(tmp_path):
    f = tmp_path / "x"
    f.mkdir()
    (f / "memories.json").write_text("{ not json", encoding="utf-8")
    out = run(f, tmp_path / "c.db", reader="claude-export")
    assert out["ok"] and any("memories.json could not be read" in r for r in out["remarks"])
