"""SPEC-2 M2 and M3: outcomes that say what happened (edited / history / unreadable), metadata that cannot be used never
blocks the text, and the Markdown reader that takes every text note of a folder (encodings, subjects, front matter)."""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
import import_readers  # noqa: E402
import md_notes  # noqa: E402
from import_core import Entry, Reader, ReadResult  # noqa: E402
from store import DecisionStore  # noqa: E402

ORDER = ["new", "version", "duplicate", "edited", "history", "refused", "unreadable", "empty", "excluded"]
SECRET = "deploy key is " + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_t3", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



@pytest.fixture(scope="module")
def import_md():
    return _load("import_md")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)
    import_core.readers()
    monkeypatch.setattr(import_core, "_READERS", dict(import_core._READERS))


def put(folder, name, text, mode="w"):
    p = Path(folder) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    return p


def preview(folder, db, **options):
    return import_core.run_import(str(folder), db, options=options)


def apply(folder, db, **options):
    return import_core.run_import(str(folder), db, apply=True, backup=False, options=options)


def by_origin(res):
    return {e["origin"]: e for e in res["entries"]}


def essence(res):
    """What an entry says about itself, without the id the note got when it was written."""
    return {e["origin"]: (e["outcome"], e["reason"], e.get("kind"), e.get("text")) for e in res["entries"]}


def texts(db):
    s = DecisionStore(str(db))
    try:
        return [r["content"] for r in s.conn.execute("SELECT content FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


def entries_of(folder, **kw):
    return {e.origin: e for e in import_readers.read_markdown(Path(folder), import_core.EmptyView(), **kw).entries}


def fake(tmp_path, entries):
    import_core.register(Reader("fake", "entries (tests)", lambda p: 3 if p.name == "fake.src" else 0,
                                lambda src, view: ReadResult(list(entries), [])))
    (tmp_path / "fake.src").write_text("x")
    return str(tmp_path / "fake.src")


# -- M2: the new outcomes ---------------------------------------------------------------------------------------------------

def test_counts_list_every_outcome_in_one_fixed_order_everywhere(tmp_path, capsys):
    db, folder = tmp_path / "x.db", tmp_path / "n"
    put(folder, "a.md", "The ferry leaves at nine.")
    res = apply(folder, db)
    assert list(res["counts"]) == ORDER and list(preview(folder, db)["counts"]) == ORDER
    assert hermind_cli.main(["import", str(folder), "--db", str(db), "--json"]) == 0
    assert list(json.loads(capsys.readouterr().out)["counts"]) == ORDER
    text = hermind_cli.import_text(preview(folder, db))
    assert ", ".join(f"{c} {k}" for k, c in zip(ORDER, [0, 0, 1, 0, 0, 0, 0, 0, 0])) in text
    assert hermind_cli.main(["import", "--list", "--db", str(db), "--json"]) == 0
    assert list(json.loads(capsys.readouterr().out)["batches"][0]["counts"]) == ORDER
    assert hermind_cli.main(["import", "--list", "--db", str(db)]) == 0
    assert "0 edited, 0 history, 0 refused, 0 unreadable" in capsys.readouterr().out


def test_edited_outcome_keeps_the_hint_that_import_md_syncs_edits(tmp_path):
    src = fake(tmp_path, [Entry("changed text", "a#1", owner_id=7, owner_relation="edited", owner_detail="differs in: content")])
    res = import_core.run_import(src, tmp_path / "x.db")
    (e,) = res["entries"]
    assert e["outcome"] == "edited" and "tools/import_md.py" in e["reason"] and "#7" in e["reason"]
    assert res["counts"]["edited"] == 1 and res["counts"]["refused"] == 0


@pytest.mark.parametrize("status", ["superseded", "merged"])
def test_history_outcome_for_older_versions_and_merged_duplicates(tmp_path, status):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "old.md", f"---\nstatus: {status}\nkind: tech\n---\nThe backups ran weekly in 2025.\n")
    put(folder, "cur.md", "---\nstatus: active\n---\nThe backups run daily since 2026.\n")
    prev = preview(folder, db)
    assert prev["counts"]["history"] == 1 and prev["counts"]["new"] == 1
    old = by_origin(prev)["old.md"]
    assert old["outcome"] == "history" and "older version kept for history" in old["reason"]
    app = apply(folder, db)
    assert essence(app) == essence(prev) and texts(db) == ["cur\n\nThe backups run daily since 2026."]


def test_unreadable_outcome_says_how_to_make_it_readable(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "bad.md", b"\xff\xfd\x80 not text")
    res = apply(folder, db)
    (e,) = res["entries"]
    assert e["outcome"] == "unreadable" and "--encoding" in e["reason"] and res["counts"]["unreadable"] == 1
    assert res["created"] == 0 and not db.exists()


def test_unusable_metadata_is_dropped_and_the_text_still_comes_in(tmp_path):
    db = tmp_path / "x.db"
    entries = [
        Entry("The Orion contract runs from March to January.", "a#1", valid_from="2027-03-01", valid_to="2026-01-01"),
        Entry("The Vega pump was serviced in spring.", "a#2", valid_from="soon", valid_to="2027-05-01"),
        Entry("The Lyra gate opens at seven.", "a#3", created="the other day", kind="gossip", trust=7),
        Entry("The Nova door sticks in summer.", "a#4", trust="high"),
        Entry("The Rigel desk is by the window.", "a#5", trust=0.9, valid_from="2026-01-01", valid_to="2026-06-01"),
    ]
    src = fake(tmp_path, entries)
    prev = import_core.run_import(src, db)
    app = import_core.run_import(src, db, apply=True, backup=False)
    assert essence(prev) == essence(app) and app["counts"]["new"] == 5 and app["created"] == 5
    r = {e["origin"]: e["reason"] for e in app["entries"]}
    assert r["a#1"] == "valid_from and valid_to dropped: valid_to is not after valid_from"
    assert r["a#2"].startswith("valid_from dropped:")
    assert "created dropped" in r["a#3"] and "kind dropped" in r["a#3"] and "trust dropped" in r["a#3"]
    assert r["a#4"].startswith("trust dropped") and r["a#5"] == ""
    con = sqlite3.connect(db)
    row = lambda frag: con.execute("SELECT valid_from, valid_to, trust, kind FROM notes WHERE content LIKE ?", (f"%{frag}%",)).fetchone()
    assert row("Orion")[:2] == (None, None)
    vf, vt, _, _ = row("Vega")
    assert vf is None and vt is not None
    assert row("Lyra")[2] == 0.5 and row("Lyra")[3] in ("chat", "state", "preference", "decision", "tech", "reference")
    assert row("Rigel")[2] == 0.9 and row("Rigel")[0] is not None


def test_a_markdown_file_with_valid_to_before_valid_from_is_new_without_either_date(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "a.md", "---\nvalid_from: 2027-03-01\nvalid_to: 2026-01-01\n---\nThe Orion contract has an odd date range.\n")
    prev = preview(folder, db)
    (e,) = prev["entries"]
    assert e["outcome"] == "new" and e["reason"] == "valid_from and valid_to dropped: valid_to is not after valid_from"
    assert "valid_to is not after valid_from" in hermind_cli.import_text(prev)
    assert essence(apply(folder, db)) == essence(prev)
    assert sqlite3.connect(db).execute("SELECT valid_from, valid_to FROM notes").fetchone() == (None, None)


# -- M3 check 1-3: which files are notes -----------------------------------------------------------------------------------

def test_md_and_txt_with_the_same_text_are_one_new_and_one_duplicate(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "note-a.md", "The Kestrel van needs new tyres in March.\n")
    put(folder, "note-a.txt", "The Kestrel van needs new tyres in March.\n")
    put(folder, "b.markdown", "The harbour office closes at five.\n")
    put(folder, "c.mdx", "The lighthouse keeper starts at dawn.\n")
    res = preview(folder, db)
    o = by_origin(res)
    assert {o["note-a.md"]["outcome"], o["note-a.txt"]["outcome"]} == {"new", "duplicate"}
    assert o["b.markdown"]["outcome"] == "new" and o["c.mdx"]["outcome"] == "new" and res["counts"]["new"] == 3


def test_hand_written_index_md_is_a_note_but_the_exporters_are_passed_by(tmp_path):
    db, out = tmp_path / "a.db", tmp_path / "out"
    s = DecisionStore(str(db))
    s.add("Nimbus backups run daily at 03:00 and keep 7 days.", "tech")
    s.add("Always answer Priya in plain English.", "preference")
    s.close()
    with contextlib.redirect_stdout(io.StringIO()):
        assert hermind_cli.main(["export", "--db", str(db), "--format", "markdown", "--out", str(out)]) == 0
    n_idx = len(list(out.rglob("index.md")))
    assert n_idx >= 2
    put(out, "extra/index.md", "The ferry timetable is pinned to the kitchen door.\n")
    res = preview(out, tmp_path / "b.db")
    origins = [e["origin"] for e in res["entries"]]
    assert "extra/index.md" in origins and not [o for o in origins if o.endswith("index.md") and o != "extra/index.md"]
    assert any(f"{n_idx} index.md" in r for r in res["remarks"])
    assert md_notes.is_export_index("# tech\n\n2 notes\n\n- [#1](00001-a.md) tech · 2026-01-01 · active — A\n")
    assert not md_notes.is_export_index("# index\n\nThe ferry timetable.\n")
    assert not md_notes.is_export_index("# tech\n\n2 notes\n\n- [#1](a.md) x\nand some prose\n")


def test_other_files_and_hidden_folders_are_counted_in_one_remark(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "a.md", "The ferry leaves at nine.\n")
    put(folder, "pic/photo.png", b"\x89PNG\r\n")
    put(folder, "data.json", "{}")
    put(folder, "more.json", "[]")
    put(folder, ".obsidian/app.json", "{}")
    put(folder, ".obsidian/themes/x.css", "a{}")
    put(folder, ".git/HEAD", "ref")
    res = preview(folder, db)
    assert [e["origin"] for e in res["entries"]] == ["a.md"]
    (remark,) = [r for r in res["remarks"] if "not text notes" in r]
    assert "3 file(s) that are not text notes" in remark and "1 .png" in remark and "2 .json" in remark and "1 .css" not in remark
    assert "3 file(s) inside hidden folders" in remark and "2 in .obsidian/" in remark and "1 in .git/" in remark


def test_import_md_and_hermind_import_walk_a_folder_with_the_same_function(tmp_path, import_md):
    folder = tmp_path / "n"
    put(folder, "a.txt", "one")
    put(folder, "sub/b.markdown", "two")
    put(folder, ".hid/c.md", "three")
    put(folder, "d.png", b"x")
    assert import_md.find_files(folder) == md_notes.walk_folder(folder).notes == [folder / "a.txt", folder / "sub" / "b.markdown"]
    ns = import_md.build_parser().parse_args(["x", "--encoding", "big5", "--ignore-file-names"])
    assert ns.encoding == "big5" and ns.ignore_file_names


# -- M3 check 4: encodings -------------------------------------------------------------------------------------------------

def test_utf16_with_a_byte_order_mark_is_imported_exactly(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    want = "The ferry leaves at nine — 渡輪九點出發。"
    put(folder, "3a7f19.md", want.encode("utf-16"))
    put(folder, "4b8e20.txt", ("Another line about the 碼頭 queue.").encode("utf-32"))
    put(folder, "5c9d31.md", b"\xef\xbb\xbf" + "UTF-8 with a mark: 準時到達。".encode())
    apply(folder, db)
    got = texts(db)
    assert want in got and "Another line about the 碼頭 queue." in got and "UTF-8 with a mark: 準時到達。" in got


def test_big5_file_is_unreadable_without_the_option_and_exact_with_it(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    want = "渡輪九點出發，請準時到碼頭。"
    put(folder, "9c2e41.txt", want.encode("big5"))
    put(folder, "7e1f52.md", "A plain UTF-8 note stays readable.\n")
    res = preview(folder, db)
    e = by_origin(res)["9c2e41.txt"]
    assert e["outcome"] == "unreadable" and "--encoding" in e["reason"]
    res = apply(folder, db, encoding="big5")
    assert by_origin(res)["9c2e41.txt"]["outcome"] == "new" and want in texts(db)
    assert "A plain UTF-8 note stays readable." in texts(db)          # the option is only for files that are not UTF-8


def test_a_file_that_still_cannot_be_decoded_is_unreadable_with_the_decoders_reason(tmp_path):
    folder, db = tmp_path / "n", tmp_path / "x.db"
    put(folder, "w.txt", b"price \x81 here")                          # 0x81 is undefined in cp1252
    e = preview(folder, db, encoding="cp1252")["entries"][0]
    assert e["outcome"] == "unreadable" and "cp1252" in e["reason"]
    put(folder, "z.txt", b"NUL \x00 inside")
    assert by_origin(preview(folder, db))["z.txt"]["outcome"] == "unreadable"


def test_unknown_encoding_name_and_option_of_another_reader_are_errors(tmp_path, capsys):
    folder = tmp_path / "n"
    put(folder, "a.md", "x y z note")
    with pytest.raises(import_core.ImportFailed, match="unknown encoding"):
        preview(folder, tmp_path / "x.db", encoding="nonsense-9")
    put(tmp_path / "h", "MEMORY.md", "one\n§\ntwo\n")
    with pytest.raises(import_core.ImportFailed, match="not used by the hermes-builtin"):
        preview(tmp_path / "h", tmp_path / "x.db", encoding="big5")
    assert hermind_cli.main(["import", str(folder), "--db", str(tmp_path / "x.db"), "--encoding", "nonsense-9"]) != 0


# -- M3 check 5: the subject --------------------------------------------------------------------------------------------------

def test_the_file_name_is_added_when_the_body_lacks_it(tmp_path):
    folder = tmp_path / "n"
    put(folder, "警報器位置.md", "在衣帽架後面")
    put(folder, "kestrel-runner.md", "Kestrel runner is the nightly job on the old server.")
    put(folder, "kestrel_nightly.md", "It is the nightly job on the old server.")
    put(folder, "untitled.md", "Body one.")
    put(folder, "20260302T1012.md", "Body two.")
    put(folder, "a8f3c2d1.md", "Body three.")
    put(folder, "550e8400-e29b-41d4-a716-446655440000.md", "Body four.")
    put(folder, "New Note.md", "Body five.")
    put(folder, "00012-wifi-password-hint.md", "It is on the router sticker.")
    put(folder, "00013-x.md", "---\nid: 13\nrev: abc123abc123\n---\nExport file body.")
    e = entries_of(folder)
    assert e["警報器位置.md"].text == "警報器位置\n\n在衣帽架後面"
    assert e["kestrel-runner.md"].text == "Kestrel runner is the nightly job on the old server."       # the words open the body
    assert e["kestrel_nightly.md"].text == "kestrel nightly\n\nIt is the nightly job on the old server."
    for name, body in (("untitled.md", "Body one."), ("20260302T1012.md", "Body two."), ("a8f3c2d1.md", "Body three."),
                       ("550e8400-e29b-41d4-a716-446655440000.md", "Body four."), ("New Note.md", "Body five.")):
        assert e[name].text == body, name
    assert e["00012-wifi-password-hint.md"].text == "wifi password hint\n\nIt is on the router sticker."
    assert e["00013-x.md"].text == "Export file body."                                               # carries id and rev


def test_title_name_and_description_are_added_when_the_body_lacks_them(tmp_path):
    folder = tmp_path / "n"
    put(folder, "x1.md", "---\ntitle: Alarm location\ndescription: Where the smoke alarm is\n---\nBehind the coat rack.")
    put(folder, "x2.md", "---\ntitle: Alarm location\ndescription: Where the smoke alarm is\n---\nThe alarm LOCATION is: behind the coat rack, "
                         "which is where the smoke alarm is.")
    put(folder, "x3.md", "---\nname: meter_cupboard\nsummary: Key is under the mat\n---\nSecond shelf.")
    put(folder, "x4.md", "---\ntitle: Gate code notes\n---\nSecond shelf.")
    e = entries_of(folder)
    assert e["x1.md"].text == "Alarm location\nWhere the smoke alarm is\n\nBehind the coat rack."
    assert e["x2.md"].text.startswith("The alarm LOCATION is:")
    assert e["x3.md"].text == "meter cupboard\nKey is under the mat\n\nSecond shelf."
    assert e["x4.md"].text == "Gate code notes\n\nSecond shelf."                                      # title wins over the file name


def test_ignore_file_names_turns_file_name_subjects_off(tmp_path):
    folder = tmp_path / "n"
    put(folder, "警報器位置.md", "在衣帽架後面")
    put(folder, "x.md", "---\ntitle: Real title\n---\nBody.")
    e = entries_of(folder, ignore_file_names=True)
    assert e["警報器位置.md"].text == "在衣帽架後面" and e["x.md"].text == "Real title\n\nBody."
    res = preview(folder, tmp_path / "x.db", ignore_file_names=True)
    assert res["counts"]["new"] == 2


# -- M3 check 6: three rules judge the body alone ------------------------------------------------------------------------------

def test_the_body_alone_decides_empty_private_and_forgotten(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "n"
    s = DecisionStore(str(db))
    s.forget(s.add("The old parking gate code was changed after the audit.", "state")[0])
    s.close()
    put(folder, "bank-pin.md", "<private>the pin is four digits</private>")
    put(folder, "警報器位置.md", "<private>behind the rack</private>")
    put(folder, "gate.md", "The old parking gate code was changed after the audit.")
    put(folder, "舊車閘.md", "The old parking gate code was changed after the audit.")
    put(folder, "empty-name.md", "")
    put(folder, "titled.md", "---\ntitle: Parking gate\n---\n")
    put(folder, "mixed-private.md", "Visible fact about the loft. <private>hidden bit</private>")
    res = preview(folder, db)
    o = by_origin(res)
    assert o["bank-pin.md"]["outcome"] == "refused" and "<private>" in o["bank-pin.md"]["reason"]
    assert o["警報器位置.md"]["outcome"] == "refused"
    assert o["gate.md"]["outcome"] == "refused" and "forgotten" in o["gate.md"]["reason"]
    assert o["舊車閘.md"]["outcome"] == "refused" and "forgotten" in o["舊車閘.md"]["reason"]
    assert o["empty-name.md"]["outcome"] == "empty" and o["titled.md"]["outcome"] == "empty"
    assert o["mixed-private.md"]["outcome"] == "new"
    assert essence(apply(folder, db)) == essence(res) and texts(db) == ["mixed private\n\nVisible fact about the loft. [REDACTED]"]


def test_duplicates_are_judged_on_the_full_text_and_a_second_import_adds_nothing(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "n"
    put(folder, "警報器位置.md", "在衣帽架後面")
    put(folder, "電錶位置.md", "在衣帽架後面")
    prev = preview(folder, db)
    assert prev["counts"]["new"] == 2 and prev["counts"]["duplicate"] == 0
    apply(folder, db)
    assert sorted(texts(db)) == sorted(["警報器位置\n\n在衣帽架後面", "電錶位置\n\n在衣帽架後面"])
    again = apply(folder, db)
    assert again["counts"]["new"] == 0 and again["counts"]["duplicate"] == 2 and again["created"] == 0


# -- M3: front matter ----------------------------------------------------------------------------------------------------------

def test_front_matter_aliases_date_pinned_trust_scope_and_unused_keys(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "n"
    put(folder, "a.md", "---\nkeywords: ferry boat\naliases: [launch, water taxi]\ndate: 2026-03-04\npinned: true\ntrust: 0.8\n"
                        "scope: work\nauthor: Maria\nmood: calm\n---\nThe ferry leaves at nine.\n")
    put(folder, "b.md", "---\nauthor: Tom\ncreated: 2026-01-02\ndate: 2026-03-04\n---\nThe harbour office closes at five.\n")
    res = apply(folder, db)
    assert res["created"] == 2
    (remark,) = [r for r in res["remarks"] if "not used" in r]
    assert "author (2 files)" in remark and "mood (1 file)" in remark
    con = sqlite3.connect(db)
    kw, pinned, trust, scope, created = con.execute(
        "SELECT keywords, pinned, trust, scope, created_at FROM notes WHERE content LIKE '%ferry leaves%'").fetchone()
    assert kw == "ferry boat launch water taxi" and pinned == 1 and trust == 0.8 and scope == "work"
    assert md_notes.iso_day(created) == "2026-03-04"
    created_b = con.execute("SELECT created_at FROM notes WHERE content LIKE '%harbour%'").fetchone()[0]
    assert md_notes.iso_day(created_b) == "2026-01-02"                      # created wins over date
    assert con.execute("SELECT scope FROM notes WHERE content LIKE '%harbour%'").fetchone()[0] == "default"


def test_an_entry_with_a_scope_is_decided_in_that_scope(tmp_path):
    db, folder = tmp_path / "x.db", tmp_path / "n"
    s = DecisionStore(str(db))
    s.add("The ferry leaves at nine.", "state", scope="work")
    s.close()
    put(folder, "a.md", "---\nscope: work\n---\nThe ferry leaves at nine.\n")
    put(folder, "b.md", "---\nscope: home\n---\nThe ferry leaves at nine.\n")
    put(folder, "c.md", "The ferry leaves at nine.\n")
    o = by_origin(preview(folder, db))
    assert o["a.md"]["outcome"] == "duplicate"
    assert o["b.md"]["outcome"] == "new"
    assert o["c.md"]["outcome"] == "new"                                   # default scope: no such note there
    apply(folder, db)
    scopes = sorted(r[0] for r in sqlite3.connect(db).execute("SELECT scope FROM notes"))
    assert scopes == ["default", "home", "work"]


# -- M3 check 7: round trip ---------------------------------------------------------------------------------------------------

def test_export_with_hidden_notes_imports_into_an_empty_database_as_the_same_notes(tmp_path):
    a_db, b_db, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out"
    s = DecisionStore(str(a_db))
    edited, _ = s.add("Nimbus backups run daily at 03:00 and keep 7 days.", "tech", "nimbus backup")
    s.update(edited, "Nimbus backups run daily at 04:00 and keep 14 days.")
    pinned, _ = s.add("Always answer Priya in plain English.", "preference", pinned=True, tags="style people")
    s.set_fields(pinned, trust=0.9)
    s.close()
    with contextlib.redirect_stdout(io.StringIO()):
        assert hermind_cli.main(["export", "--db", str(a_db), "--format", "markdown", "--out", str(out),
                                 "--include-history"]) == 0
    assert len([p for p in out.rglob("*.md") if p.name != "index.md"]) == 3
    prev = preview(out, b_db)
    assert prev["counts"]["new"] == 2 and prev["counts"]["history"] == 1
    hist = next(e for e in prev["entries"] if e["outcome"] == "history")
    assert "older version" in hist["reason"]
    res = apply(out, b_db)
    assert res["created"] == 2
    con = sqlite3.connect(b_db)
    live = con.execute("SELECT content, pinned, trust, tags, kind, keywords FROM notes WHERE status='active' ORDER BY id").fetchall()
    assert len(live) == 2 and con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
    by_text = {r[0]: r for r in live}
    assert "Nimbus backups run daily at 03:00 and keep 7 days." not in by_text
    n = by_text["Nimbus backups run daily at 04:00 and keep 14 days."]
    assert n[1] == 0 and n[4] == "tech"
    p = by_text["Always answer Priya in plain English."]
    assert p[1] == 1 and p[2] == 0.9 and set(p[3].split()) >= {"style", "people"} and p[4] == "preference"
    assert not [e for e in apply(out, b_db)["entries"] if e["outcome"] == "new"]            # and again: nothing new


# -- M3 check 8: tools/import_md.py ------------------------------------------------------------------------------------------

def _summary(text):
    """The numbers of the tool's summary line, under the names its dry run and its --apply share."""
    fix = lambda k: k.strip().replace("to ", "").replace("created", "new").replace("updated", "update")
    return {fix(k): int(n) for n, k in re.findall(r"(\d+) ([a-z() ]+?)(?:,|$|\n)", text.split("summary:")[1])}


def _run_tool(import_md, db, folder, *flags):
    buf = io.StringIO()
    code = import_md.run(import_md.build_parser().parse_args(["--db", str(db), str(folder), *flags]), out=buf)
    return code, buf.getvalue()


def test_import_md_dry_run_counts_equal_what_apply_produces_on_the_new_cases(tmp_path, import_md):
    base = tmp_path / "base.db"
    s = DecisionStore(str(base))
    s.add("The office printer lives on the second floor next to the kitchen.", "state")
    s.forget(s.add("The old parking gate code was changed after the audit.", "state")[0])
    s.close()
    folder = tmp_path / "mixed"
    put(folder, "a.txt", "Maria prefers vegetarian lunches on team days.")
    put(folder, "a.md", "Maria prefers vegetarian lunches on team days.")
    put(folder, "警報器位置.md", "在衣帽架後面")
    put(folder, "電錶位置.md", "在衣帽架後面")
    put(folder, "old.md", "---\nstatus: superseded\n---\nAn old fact about the roof.")
    put(folder, "bad.md", b"\xff\xfd\x80 junk")
    put(folder, "big5.txt", "渡輪九點出發，請準時到碼頭。".encode("big5"))
    put(folder, "dates.md", "---\nvalid_from: 2027-03-01\nvalid_to: 2026-01-01\nkind: gossip\ntrust: 7\n---\nThe Orion contract has odd dates.")
    put(folder, "bank-pin.md", "<private>pin</private>")
    put(folder, "gate.md", "The old parking gate code was changed after the audit.")
    put(folder, "index.md", "The ferry timetable is on the kitchen door.")
    put(folder, "pic.png", b"x")
    put(folder, ".git/config", "x")
    for flags, options in (([], {}), (["--encoding", "big5"], {"encoding": "big5"}),
                           (["--ignore-file-names"], {"ignore_file_names": True})):
        dry_db, apply_db = tmp_path / "dry.db", tmp_path / "apply.db"
        for d in (dry_db, apply_db):
            shutil.copy(base, d)
        before = dry_db.read_bytes()
        _, dry = _run_tool(import_md, dry_db, folder, *flags)
        assert dry_db.read_bytes() == before
        _, applied = _run_tool(import_md, apply_db, folder, "--apply", *flags)
        assert _summary(dry) == _summary(applied), (dry, applied)
        n_new = _summary(dry)["new"]
        assert n_new > 0 and len(texts(apply_db)) == 1 + n_new
        # `hermind import` makes the same decisions about which files become notes
        assert import_core.run_import(str(folder), base, options=options)["counts"]["new"] == n_new, (flags, dry)
    _, dry = _run_tool(import_md, dry_db, folder)
    assert "unreadable" in dry and _summary(dry)["invalid"] == 2                 # bad.md and big5.txt
    assert _summary(_run_tool(import_md, dry_db, folder, "--encoding", "big5")[1])["invalid"] == 1
