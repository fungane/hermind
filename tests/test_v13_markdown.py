"""`hermind export --format markdown` + tools/import_md.py (Markdown round trip)."""
import contextlib
import importlib.util
import io
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()
DAY = 86400.0


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_tool", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tools(pkg):
    import md_notes
    return None, _load("import_md"), md_notes


def _seed(s):
    ids = {}
    ids["a"] = s.add("Zorblax 備份每日 03:00 執行，保留 7 日", kind="tech", keywords="zorblax backup 備份", tags="ops",
                     entities=["Zorblax"], created_at=NOW - 9 * DAY)[0]
    ids["b"] = s.add("Zorblax 備份上傳到 R2 儲存桶\n\n- 區域: apac\n- 另見: ---\n- 說明: key: value", kind="tech",
                     entities=["Zorblax"], created_at=NOW - 8 * DAY)[0]
    ids["pref"] = s.add("永遠用繁體中文回覆", kind="preference", pinned=True, created_at=NOW - 7 * DAY)[0]
    ids["old"] = s.add("目前 Zephyrbox 硬碟剩 13GB，記憶體剩 800MB", kind="state", entities=["Zephyrbox"],
                       created_at=NOW - 6 * DAY)[0]
    ids["new"] = s.add("目前 Zephyrbox 硬碟剩 21GB，記憶體剩 1.2GB", kind="state", entities=["Zephyrbox"],
                       created_at=NOW - 1 * DAY)[0]
    ids["pending"] = s.add("LLM 抽取：VPS 有 4GB swap 並且啟用 zram", kind="tech", review="pending",
                           created_at=NOW - 3600)[0]
    s.consolidate(now=NOW)
    return ids


@pytest.fixture
def env(tmp_path, store_mod, tools):
    path = tmp_path / "t.db"
    s = store_mod.DecisionStore(str(path))
    ids = _seed(s)
    s.close()
    return path, tmp_path / "out", ids


def _export(tools, db, out, *flags):
    import hermind_cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = hermind_cli.main(["export", "--db", str(db), "--format", "markdown", "--out", str(out), *flags])
    return code, buf.getvalue()


def _import(tools, db, folder, *flags):
    imp = tools[1]
    buf = io.StringIO()
    code = imp.run(imp.build_parser().parse_args(["--db", str(db), str(folder), *flags]), out=buf)
    return code, buf.getvalue()


def _state(store_mod, db):
    s = store_mod.DecisionStore(str(db))
    rows = [tuple(r) for r in s.conn.execute("SELECT * FROM notes ORDER BY id")]
    links = [tuple(r) for r in s.conn.execute("SELECT * FROM note_links ORDER BY src, dst, rel")]
    s.close()
    return rows, links


def _file(out, nid):
    return next(out.rglob(f"{nid:05d}-*.md"))


# -- format ---------------------------------------------------------------------------------------------------------

def test_front_matter_round_trips_awkward_values(tools):
    md = tools[2]
    note = {"id": 7, "content": "第一行: 有冒號\n---\n不是結尾\n```code```", "kind": "tech", "keywords": 'a "quoted" 詞 b',
            "tags": "x y", "trust": 0.5, "created_at": NOW, "valid_from": None, "valid_to": NOW, "review": "active",
            "pinned": 0, "scope": "default"}
    text = md.render(note, {"status": "active", "supersedes": [3, 4], "outdated_by": None})
    meta, body = md.parse_note_file(text)
    assert body == note["content"]
    assert meta["id"] == 7 and meta["kind"] == "tech" and meta["tags"] == ["x", "y"] and meta["supersedes"] == [3, 4]
    assert meta["keywords"] == 'a "quoted" 詞 b' and meta["valid_from"] is None and meta["pinned"] is False
    assert md.file_fields(meta, body) == md.note_fields(note)
    assert meta["rev"] == md.rev_of(md.note_fields(note))


def test_parser_is_forgiving_for_hand_written_files(tools):
    md = tools[2]
    meta, body = md.parse_note_file("---\nid: 5\ntags: [alpha, beta]   # my tags\nkind: Decision\ntrust: 0.8\n---\nbody text\n")
    assert meta == {"id": 5, "tags": ["alpha", "beta"], "kind": "Decision", "trust": 0.8} and body == "body text"
    assert md.parse_note_file("just text\nno front matter") == ({}, "just text\nno front matter")
    assert md.parse_note_file("---\nid: 1\nnever closed\n") == ({}, "---\nid: 1\nnever closed")


# -- export -------------------------------------------------------------------------------------------------------------

def test_export_by_kind_layout_front_matter_and_indexes(tools, env):
    db, out, ids = env
    code, text = _export(tools, db, out)
    assert code == 0 and "exported 5 notes" in text            # 6 active+pending minus the superseded Zephyrbox note
    assert {p.name for p in out.iterdir() if p.is_dir()} == {"tech", "preference", "state"}
    md = tools[2]
    meta, body = md.parse_note_file(_file(out, ids["a"]).read_text(encoding="utf-8"))
    assert meta["kind"] == "tech" and meta["status"] == "active" and meta["tags"] == ["ops"]
    assert meta["trust"] == 0.5 and meta["created"] == time.strftime("%Y-%m-%d", time.localtime(NOW - 9 * DAY))
    assert meta["valid_from"] is None and meta["valid_to"] is None and meta["keywords"] == "zorblax backup 備份"
    assert body.startswith("Zorblax 備份每日")
    new_meta, _ = md.parse_note_file(_file(out, ids["new"]).read_text(encoding="utf-8"))
    assert new_meta["supersedes"] == [ids["old"]] and new_meta["valid_from"] is not None
    pend, _ = md.parse_note_file(_file(out, ids["pending"]).read_text(encoding="utf-8"))
    assert pend["status"] == "pending"
    idx = (out / "tech" / "index.md").read_text(encoding="utf-8")
    assert f"[#{ids['a']}]" in idx and "Zorblax 備份每日" in idx and "pending" in idx
    assert "tech/index.md" in (out / "index.md").read_text(encoding="utf-8")
    assert not list(out.rglob(f"{ids['old']:05d}-*.md"))       # superseded notes stay out by default


def test_export_include_hidden_marks_status(tools, env):
    db, out, ids = env
    _export(tools, db, out, "--include-history")
    meta, _ = tools[2].parse_note_file(_file(out, ids["old"]).read_text(encoding="utf-8"))
    assert meta["status"] == "superseded" and meta["outdated_by"] == ids["new"]


def test_export_by_entity_layout(tools, env):
    db, out, ids = env
    _export(tools, db, out, "--by", "entity")
    assert _file(out, ids["a"]).parent.parent.name == "default"
    assert _file(out, ids["a"]).parent.name == _file(out, ids["b"]).parent.name       # both about Zorblax
    assert _file(out, ids["pref"]).parent.name == "_misc"


def test_export_refuses_non_empty_dir_and_force_is_idempotent(tools, env):
    db, out, ids = env
    _export(tools, db, out)
    snap = {p: p.read_text(encoding="utf-8") for p in out.rglob("*.md")}
    assert _export(tools, db, out)[0] != 0          # a non-empty folder is refused
    assert _export(tools, db, out, "--force")[0] == 0
    assert {p: p.read_text(encoding="utf-8") for p in out.rglob("*.md")} == snap


def test_export_paths_stay_inside_out_dir(tools, env, store_mod):
    db, out, ids = env
    s = store_mod.DecisionStore(str(db))
    s.add("../../../../etc/passwd 之類的路徑 \\..\\ 也不可以跑出去", kind="chat")
    s.add("/absolute/path/start 與 con: aux* ?<>| 特殊字元", kind="chat")
    s.close()
    _export(tools, db, out)
    root = out.resolve()
    assert all(root in p.resolve().parents for p in out.rglob("*") if p.is_file())
    assert not (out.parent / "etc").exists()


# -- import: the round trip ---------------------------------------------------------------------------------------------

def test_round_trip_without_edits_changes_nothing(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    before = _state(store_mod, db)
    code, text = _import(tools, db, out)
    assert code == 0 and "DRY RUN" in text and "no changes" in text and "5 unchanged" in text
    code, text = _import(tools, db, out, "--apply")
    assert code == 0 and "APPLIED" in text and "no changes" in text
    assert _state(store_mod, db) == before                       # byte-for-byte the same rows and links


def test_edit_text_goes_through_versioned_update(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    f = _file(out, ids["a"])
    f.write_text(f.read_text(encoding="utf-8").replace("保留 7 日", "保留 14 日"), encoding="utf-8")
    before = _state(store_mod, db)
    edited = f.read_text(encoding="utf-8")
    code, text = _import(tools, db, out)                         # dry run first
    assert f"#{ids['a']}" in text and "content" in text and "1 to update" in text
    assert _state(store_mod, db) == before and f.read_text(encoding="utf-8") == edited      # nothing touched
    code, text = _import(tools, db, out, "--apply")
    assert code == 0 and "1 updated" in text
    s = store_mod.DecisionStore(str(db))
    try:
        assert "保留 14 日" in s.get(ids["a"])["content"]
        hist = s.history(ids["a"])
        assert hist and "保留 7 日" in hist[0]["content"]          # the old text is kept as history
        assert [h["id"] for h in s.search("zorblax 保留 14 日")][0] == ids["a"]
        assert s.get(ids["a"])["valid_from"] is not None          # versioned update opened the new validity
    finally:
        s.close()
    # the exported file now carries a stale rev, but it equals the database again: a second import is a no-op
    code, text = _import(tools, db, out, "--apply")
    assert "no changes" in text


def test_edit_metadata_fields(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    f = _file(out, ids["a"])
    t = f.read_text(encoding="utf-8")
    t = t.replace("tags: [\"ops\"]", 'tags: ["ops", "backup"]').replace("trust: 0.5", "trust: 0.8")
    t = t.replace("valid_to:\n", "valid_to: 2027-01-31\n").replace("kind: tech", "kind: decision")
    f.write_text(t, encoding="utf-8")
    code, text = _import(tools, db, out, "--apply")
    assert code == 0 and "kind" in text and "tags" in text and "trust" in text and "valid_to" in text
    s = store_mod.DecisionStore(str(db))
    try:
        r = s.get(ids["a"])
        assert r["kind"] == "decision" and r["tags"] == "backup ops" and r["trust"] == 0.8
        assert time.strftime("%Y-%m-%d", time.localtime(r["valid_to"])) == "2027-01-31"
        assert r["content"].startswith("Zorblax 備份每日") and not s.history(ids["a"])      # metadata: no history row
    finally:
        s.close()


def test_status_active_confirms_a_pending_note(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    f = _file(out, ids["pending"])
    f.write_text(f.read_text(encoding="utf-8").replace("status: pending", "status: active"), encoding="utf-8")
    assert _import(tools, db, out, "--apply")[0] == 0
    s = store_mod.DecisionStore(str(db))
    assert s.get(ids["pending"])["review"] == "active"
    s.close()


def test_new_file_without_id_becomes_a_note_and_can_be_stamped(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    new_file = out / "tech" / "mine.md"
    new_file.write_text("---\nkind: decision\ntags: [mine]\nkeywords: \"手寫 note\"\n---\n決定每週日做一次異地備份\n", encoding="utf-8")
    plain = out / "plain.md"
    plain.write_text("只是一段普通文字，沒有 front matter，也要能匯入\n", encoding="utf-8")
    code, text = _import(tools, db, out)
    assert "2 new" in text and "mine.md" in text
    s = store_mod.DecisionStore(str(db)); n0 = s.count(); s.close()
    code, text = _import(tools, db, out, "--apply", "--stamp-ids")
    assert code == 0 and "2 created" in text
    s = store_mod.DecisionStore(str(db))
    try:
        assert s.count() == n0 + 2
        hit = [h for h in s.search("每週日 異地備份") if "異地備份" in h["content"]][0]
        assert hit["kind"] == "decision" and hit["tags"] == "mine" and "手寫" in hit["keywords"]
        assert [h for h in s.search("普通文字 front matter") if "沒有 front matter" in h["content"]]
    finally:
        s.close()
    assert tools[2].parse_note_file(new_file.read_text(encoding="utf-8"))[0]["id"] == hit["id"]      # stamped
    assert "no changes" in _import(tools, db, out, "--apply")[1]                                      # not new again


def test_without_stamping_a_second_import_finds_duplicates_not_new_notes(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    (out / "extra.md").write_text("唯一的新筆記內容 xyzzy 測試\n", encoding="utf-8")
    _import(tools, db, out, "--apply")
    s = store_mod.DecisionStore(str(db)); n = s.count(); s.close()
    code, text = _import(tools, db, out, "--apply")
    s = store_mod.DecisionStore(str(db))
    assert s.count() == n and "same text as" in text
    s.close()


def test_conflict_when_file_and_database_both_changed(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    f = _file(out, ids["a"])
    f.write_text(f.read_text(encoding="utf-8").replace("保留 7 日", "保留 30 日"), encoding="utf-8")
    s = store_mod.DecisionStore(str(db))
    s.update(ids["a"], "Zorblax 備份每日 04:00 執行，保留 7 日")           # database moved on after the export
    s.close()
    before = _state(store_mod, db)
    code, text = _import(tools, db, out, "--apply")
    assert code == 1 and "conflict" in text and "1 conflict" in text
    assert _state(store_mod, db) == before
    code, text = _import(tools, db, out, "--apply", "--force")
    assert code == 0
    s = store_mod.DecisionStore(str(db))
    assert "保留 30 日" in s.get(ids["a"])["content"]
    s.close()


def test_untouched_old_export_does_not_overwrite_newer_database(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    s = store_mod.DecisionStore(str(db))
    s.update(ids["a"], "Zorblax 備份每日 04:00 執行，保留 7 日")
    s.close()
    before = _state(store_mod, db)
    code, text = _import(tools, db, out, "--apply")
    assert code == 0 and "0 conflict(s)" in text
    assert _state(store_mod, db) == before


def test_skips_and_refusals(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out, "--include-history")
    (out / "secret.md").write_text("連線用的 token 是 ghp_" + "a1B2c3D4" * 5 + "\n", encoding="utf-8")
    dup = _file(out, ids["b"])
    (out / "dup.md").write_text(dup.read_text(encoding="utf-8"), encoding="utf-8")
    (out / "bad.md").write_bytes(b"\xff\xfd\x80 not utf8")   # (a file starting FF FE is UTF-16 with a byte-order mark, which is read)
    (out / ".hidden").mkdir()
    (out / ".hidden" / "x.md").write_text("should be ignored\n", encoding="utf-8")
    old = _file(out, ids["old"])
    old.write_text(old.read_text(encoding="utf-8").replace("13GB", "99GB"), encoding="utf-8")        # superseded note
    before = _state(store_mod, db)
    code, text = _import(tools, db, out, "--apply")
    assert code == 1
    for why in ("refused", "already seen", "unreadable", "is superseded"):
        assert why in text, why
    assert "x.md" not in text and "index.md" not in text
    assert _state(store_mod, db) == before


def test_empty_body_is_invalid_not_a_deletion(tools, env, store_mod):
    db, out, ids = env
    _export(tools, db, out)
    f = _file(out, ids["pref"])
    meta_only = f.read_text(encoding="utf-8").split("---\n", 2)
    f.write_text("---\n" + meta_only[1] + "---\n\n", encoding="utf-8")
    before = _state(store_mod, db)
    code, text = _import(tools, db, out, "--apply")
    assert code == 1 and "empty text" in text and _state(store_mod, db) == before


def test_partial_front_matter_does_not_clear_other_fields(tools, env, store_mod):
    db, out, ids = env
    s = store_mod.DecisionStore(str(db))
    s.set_fields(ids["a"], tags="ops keep", trust=0.9, valid_to="2027-05-05")
    rev = tools[2].rev_of(tools[2].note_fields(s.get(ids["a"])))      # a file belongs to a note only with its id AND a matching rev
    s.close()
    out.mkdir()
    (out / "one.md").write_text(f"---\nid: {ids['a']}\nrev: {rev}\n---\nZorblax 備份改為每日 05:00 執行，保留 7 日\n", encoding="utf-8")
    assert _import(tools, db, out, "--apply")[0] == 0
    s = store_mod.DecisionStore(str(db))
    try:
        r = s.get(ids["a"])
        assert "05:00" in r["content"] and r["tags"] == "ops keep" and r["trust"] == 0.9 and r["valid_to"] is not None
        assert r["keywords"] == "zorblax backup 備份"
    finally:
        s.close()


def test_missing_paths_are_errors(tools, tmp_path):
    imp = tools[1]
    assert imp.run(imp.build_parser().parse_args(["--db", str(tmp_path / "nope.db"), str(tmp_path)]), out=io.StringIO()) == 2


def test_set_fields_validation(store):
    nid, _ = store.add("Quuxlet 預設 port 為 8080", kind="tech")
    assert store.set_fields(nid, tags="b a", trust=5, valid_from="2026-01-01") is True
    r = store.get(nid)
    assert r["tags"] == "b a" and r["trust"] == 1.0 and r["valid_from"] is not None
    with pytest.raises(ValueError):
        store.set_fields(nid, kind="nonsense")
    with pytest.raises(ValueError):
        store.set_fields(nid, valid_to="2025-01-01")             # before valid_from
    assert store.set_fields(nid, valid_from=None) is True and store.get(nid)["valid_from"] is None
    assert store.set_fields(99999, tags="x") is False
