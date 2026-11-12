"""Notes imported from other tools stay out of the core block (store_core.py) until they are used."""

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
from store import DecisionStore  # noqa: E402
from store_common import IMPORT_HERMIND  # noqa: E402

NOW = time.time()
RECENT = time.strftime("%Y-%m-%d", time.localtime(NOW - 2 * 86400))      # newer than every native note below


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def native_db(path: Path, n: int = 270) -> Path:
    s = DecisionStore(str(path))
    for i in range(n):
        s.add(f"The user prefers option number {i} for report layout {i * 7}.", kind="preference",
              created_at=NOW - 86400 * (10 + n - i), source_session="native")
    s.close()
    return path


def block(db: Path) -> str:
    s = DecisionStore(str(db))
    try:
        return s.core_summary()
    finally:
        s.close()


def w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def claude_folder(root: Path) -> Path:
    mem = root / "S" / "projects" / "-nowhere-atlas" / "memory"
    w(mem / "MEMORY.md", "- [Style](style.md) — answer style\n- Prefers dark mode in every editor\n")
    w(mem / "style.md", "---\nname: style\ntype: feedback\n---\nThe user wants answers in British English.\n")
    w(mem / "wiki.md", "---\ntype: user\n---\nThe user keeps a private wiki about gauges.\n")
    w(root / "S" / "CLAUDE.md", "# Me\nPrefer short answers.\n")
    return root / "S"


def chatgpt_export(root: Path) -> Path:
    c = {"id": "c1", "create_time": 1_760_000_000.0, "update_time": 1_760_000_100.0,
         "mapping": {"a": {"id": "a", "parent": None, "children": [], "message": {
             "author": {"role": "user"}, "create_time": None,
             "content": {"content_type": "model_editable_context",
                         "model_set_context": f"1. [{RECENT}]. User grows chillies on the balcony.\n2. [{RECENT}]. User's sister is called Ines."}}}}}
    return w(root / "export" / "conversations.json", json.dumps([c])).parent


def test_the_core_block_is_identical_after_importing_a_claude_code_folder_and_a_chatgpt_export(tmp_path):
    db = native_db(tmp_path / "n.db")
    before = block(db)
    assert before and "(+" in before                                  # 270 preferences do not all fit: the block is full
    a = import_core.run_import(str(claude_folder(tmp_path)), db, apply=True, backup=False)
    b = import_core.run_import(str(chatgpt_export(tmp_path)), db, apply=True, backup=False)
    assert a["reader"] == "claude-code" and b["reader"] == "chatgpt-export" and a["created"] >= 4 and b["created"] == 2
    s = DecisionStore(str(db))
    prefs = s.conn.execute("SELECT COUNT(*) FROM notes WHERE kind='preference' AND source_session LIKE 'import:%'").fetchone()[0]
    s.close()
    assert prefs >= 6                                                  # the import did add preferences ...
    assert block(db) == before                                         # ... and none of them is in the block
    s = DecisionStore(str(db))                                         # (without the rule they would be: they are the newest)
    s.conn.execute("UPDATE notes SET source_session='native' WHERE source_session LIKE 'import:%'")
    s.conn.commit()
    s.close()
    assert block(db) != before


def imported_pref_db(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    db = native_db(tmp_path / "n.db")
    import_core.run_import(str(chatgpt_export(tmp_path)), db, apply=True, backup=False)
    return db


def set_note(db, text, **cols):
    s = DecisionStore(str(db))
    sets = ", ".join(f"{k}=?" for k in cols)
    s.conn.execute(f"UPDATE notes SET {sets} WHERE content=?", [*cols.values(), text])
    s.conn.commit()
    s.close()


def test_an_imported_preference_enters_the_block_once_it_is_used_pinned_or_trusted(tmp_path):
    text = "User grows chillies on the balcony."
    db = imported_pref_db(tmp_path)
    assert text not in block(db)
    set_note(db, text, access_count=1)
    assert f"- {text}" in block(db).split("Preferences:")[1]
    db2 = imported_pref_db(tmp_path / "p")
    set_note(db2, text, pinned=1)
    assert f"- {text}" in block(db2).split("Pinned:")[1].split("Preferences:")[0]
    db3 = imported_pref_db(tmp_path / "t")
    set_note(db3, text, trust=0.6)                                      # positive feedback raises trust above the default
    assert f"- {text}" in block(db3)
    db4 = imported_pref_db(tmp_path / "f")
    assert text not in block(db4)


def test_a_preference_from_a_hermind_markdown_export_is_treated_like_a_native_one(tmp_path):
    db = native_db(tmp_path / "n.db")
    folder = tmp_path / "md"
    w(folder / "00007-layout.md", f"---\nid: 7\nkind: preference\nstatus: active\ntrust: 0.5\ncreated: {RECENT}\nrev: 0a1b2c3d4e5f\n---\n"
                                 "The user keeps the sidebar closed.\n")
    w(folder / "plain.md", "---\nkind: preference\n---\nThe user likes monospace fonts.\n")
    out = import_core.run_import(str(folder), db, apply=True, backup=False)
    assert out["counts"]["new"] == 2
    s = DecisionStore(str(db))
    sessions = dict(s.conn.execute("SELECT content, source_session FROM notes WHERE source_session LIKE 'import:%'").fetchall())
    s.close()
    assert sessions["The user keeps the sidebar closed."] == IMPORT_HERMIND == "import:hermind"
    assert sessions[next(k for k in sessions if "monospace" in k)] == "import:markdown"
    text = block(db)
    assert "- The user keeps the sidebar closed." in text and "monospace" not in text


def test_search_and_recall_are_not_changed_by_the_rule(tmp_path):
    db = imported_pref_db(tmp_path)
    s = DecisionStore(str(db))
    try:
        hits = s.search("chillies balcony", limit=5)
    finally:
        s.close()
    assert any("chillies" in (h["content"] if isinstance(h, dict) else str(h)) for h in hits)
