"""The `chatgpt-export` reader of `hermind import` (synthetic exports: the real format is not published)."""

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402
import reader_exports as rx  # noqa: E402
from store import DecisionStore  # noqa: E402

T1, T2, T3 = 1_740_000_000.0, 1_750_000_000.0, 1_760_000_000.0      # three conversations at three times (2025)
A1 = "User grows chillies on the balcony."
A2 = "User's sister is called Ines."
A3 = "User drives a blue Fiat."
A4 = "User is learning Portuguese."
A5 = "User plays the cello on Sundays."
W_OLD = "User has a cat called Pixel."
W_NEW = "User is training for a 10 km run in June."
CI_OLD = ("I teach chemistry in Porto.", "Answer in British English.")
CI_NEW = ("I teach physics in Lisbon.", "Answer in plain English, briefly.")
FENCE = "`" * 3


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


def msg(role="user", content=None, t=None, recipient="all", metadata=None):
    return {"author": {"role": role}, "create_time": t, "recipient": recipient, "metadata": metadata or {},
            "content": content if content is not None else {"content_type": "text", "parts": ["hello"]}}


def mlist(*items, dated=True, **extra):
    lines = [f"{i}. [2026-03-0{i}]. {t}" if dated else f"{i}. {t}" for i, t in enumerate(items, 1)]
    return {"content_type": "model_editable_context", "model_set_context": "\n".join(lines), "repository": None,
            "repo_summary": None, "structured_context": None, **extra}


def fenced_ci(about, how):
    return {"content_type": "user_editable_context",
            "user_profile": f"The user provided the following information about themselves.\nUser profile:\n{FENCE}{about}{FENCE}",
            "user_instructions": f"The user provided the additional info about how they would like you to respond:\n{FENCE}{how}{FENCE}"}


def conv(cid, ctime, messages, side=()):
    """A conversation whose mapping is a chain of *messages*; *side* messages hang on a branch that current_node skips."""
    mapping = {"root": {"id": "root", "message": None, "parent": None, "children": []}}
    prev = "root"
    for i, m in enumerate(messages):
        nid = f"n{i}"
        mapping[nid] = {"id": nid, "message": m, "parent": prev, "children": []}
        mapping[prev]["children"].append(nid)
        prev = nid
    for j, m in enumerate(side):
        mapping[f"s{j}"] = {"id": f"s{j}", "message": m, "parent": "n0", "children": []}
        mapping["n0"]["children"].append(f"s{j}")
    return {"id": cid, "title": f"chat {cid}", "create_time": ctime, "update_time": (ctime or 0) + 10, "mapping": mapping, "current_node": prev}


def three_conversations():
    c1 = conv("c1", T1, [msg(content=mlist(A1, A2, A3)), msg(content=fenced_ci(*CI_OLD)),
                         msg(t=T1 + 1, content={"content_type": "text", "parts": ["What is the capital of Peru?"]})])
    c2 = conv("c2", T2, [msg("assistant", {"content_type": "text", "parts": [A1]}, t=T2 + 5, recipient="bio"),
                         msg("assistant", {"content_type": "text", "parts": [W_OLD]}, t=T2 + 6, recipient="bio.update"),
                         msg("assistant", {"content_type": "text", "parts": ["Peru's capital is Lima."]}, t=T2 + 7)])
    ci_new = msg(content={"content_type": "user_editable_context"},
                 metadata={"user_context_message_data": {"about_user_message": CI_NEW[0], "about_model_message": CI_NEW[1]}})
    c3 = conv("c3", T3, [msg(content=mlist(A1, A2, A4, A5)), ci_new],
              side=[msg("assistant", {"content_type": "text", "parts": [W_NEW]}, t=T3 + 100, recipient="bio")])
    return [c1, c2, c3]


EXTRA_FILES = {"chat.html": "<html>chats</html>", "user.json": '{"id": "u1", "email": "someone"}', "message_feedback.json": "[]",
               "dalle/img1.png": "png", "dalle/img2.png": "png", "shared/x.json": "{}"}


def write_folder(root: Path, convs, split=False, extra=True):
    root.mkdir(parents=True, exist_ok=True)
    if split:
        half = len(convs) // 2
        (root / "conversations-000.json").write_text(json.dumps(convs[:half]), encoding="utf-8")
        (root / "conversations-001.json").write_text(json.dumps(convs[half:]), encoding="utf-8")
    else:
        (root / "conversations.json").write_text(json.dumps(convs), encoding="utf-8")
    for name, text in (EXTRA_FILES.items() if extra else ()):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def write_zip(path: Path, convs, extra=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(convs))
        for name, text in (EXTRA_FILES.items() if extra else ()):
            zf.writestr(name, text)
    return path


def run(src, db, **kw):
    return import_core.run_import(str(src), db, **kw)


def view(out):
    return [(e["origin"], e["outcome"], e["reason"], e.get("text", ""), e.get("kind", "")) for e in out["entries"]]


@pytest.fixture
def export(tmp_path):
    return write_folder(tmp_path / "export", three_conversations())


def outcome(out, origin):
    return next(e for e in out["entries"] if e["origin"] == origin)


def stored(db):
    s = DecisionStore(str(db))
    try:
        return [r["content"] for r in s.conn.execute("SELECT content FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


# -- check 1: memory lists --------------------------------------------------------------------------------------------------

def test_the_newest_list_gives_four_new_entries_and_the_removed_item_is_history(tmp_path, export):
    out = run(export, tmp_path / "c.db")
    assert out["reader"] == "chatgpt-export" and out["reader_detected"]
    lst = [e for e in out["entries"] if e["origin"].startswith("memory#")]
    assert [(e["origin"], e["outcome"]) for e in lst] == [(f"memory#{i}", "new") for i in range(1, 5)]
    assert [e["text"] for e in lst] == [A1, A2, A4, A5]
    old = [e for e in out["entries"] if e["origin"].startswith("memory-history/")]
    assert len(old) == 1 and old[0]["outcome"] == "history" and old[0]["origin"] == "memory-history/2025-02-19#3"
    assert "removed or reworded in ChatGPT later" in old[0]["reason"] and "2025-02-19" in old[0]["reason"]
    assert all(e["kind"] == "preference" for e in out["entries"] if e["outcome"] == "new")
    assert out["entries"][0]["origin"] == "memory#1"                        # the newest list's items come first


def test_list_item_dates_become_the_created_date_and_texts_are_exact(tmp_path, export):
    import time as _t
    run(export, tmp_path / "c.db", apply=True, backup=False)
    s = DecisionStore(str(tmp_path / "c.db"))
    try:
        row = s.conn.execute("SELECT created_at, content FROM notes WHERE content=?", (A1,)).fetchone()
    finally:
        s.close()
    assert _t.strftime("%Y-%m-%d", _t.localtime(row["created_at"])) == "2026-03-01"
    texts = stored(tmp_path / "c.db")
    assert {A1, A2, A4, A5, W_NEW} <= set(texts) and len(texts) == 7 and A3 not in texts and W_OLD not in texts


@pytest.mark.parametrize("text,expected", [
    ("1. [2026-03-02]. User grows chillies.\n2. [2026-04-11]. User's sister is Ines.", [("2026-03-02", "User grows chillies."), ("2026-04-11", "User's sister is Ines.")]),
    ("1. User grows chillies.\n2. [2026-04-11] No dot after the bracket.", [(None, "User grows chillies."), ("2026-04-11", "No dot after the bracket.")]),
    ("1. First item\ncontinues here.\n2. Second", [(None, "First item\ncontinues here."), (None, "Second")]),
    ("plain line one\n\nplain line two\n", [(None, "plain line one"), (None, "plain line two")]),
    ("", []),
])
def test_memory_list_item_forms(text, expected):
    assert rx.parse_memory_list(text) == expected


# -- check 2: memory writes -------------------------------------------------------------------------------------------------

def test_memory_writes_duplicate_history_and_a_side_branch_write(tmp_path, export):
    out = run(export, tmp_path / "c.db")
    writes = [e for e in out["entries"] if e["origin"].startswith("memory-write/")]
    by_text = {}
    for e in writes:
        by_text[e["origin"]] = e
    dup = next(e for e in writes if e["origin"] == "memory-write/2025-06-15#1")
    assert dup["outcome"] == "duplicate" and "memory#1" in dup["reason"]                     # equals a1: a duplicate of the list item
    old = next(e for e in writes if e["origin"] == "memory-write/2025-06-15#2")
    assert old["outcome"] == "history" and "a memory write from 2025-06-15 that is not in ChatGPT's newest memory list (2025-10-09)" in old["reason"]
    new = next(e for e in writes if e["origin"].startswith("memory-write/2025-10-09"))
    assert new["outcome"] == "new" and new["text"] == W_NEW                                  # later than the list, in a side branch
    assert not any("Lima" in e.get("text", "") or "Peru" in e.get("text", "") for e in out["entries"])   # chat text is not memory
    assert out["counts"]["new"] == 4 + 1 + 2 and out["counts"]["history"] == 1 + 1 + 2       # a3, the old write, the two old custom instructions
    assert out["counts"]["duplicate"] == 1


# -- check 3: custom instructions -------------------------------------------------------------------------------------------

def test_custom_instructions_two_versions(tmp_path, export):
    out = run(export, tmp_path / "c.db", apply=True, backup=False)
    new = {e["origin"]: e for e in out["entries"] if e["origin"].startswith("custom-instructions/")}
    assert set(new) == {"custom-instructions/about-you", "custom-instructions/how-to-respond"}
    assert all(e["outcome"] == "new" for e in new.values())
    texts = stored(tmp_path / "c.db")
    assert f"ChatGPT custom instructions › about you\n\n{CI_NEW[0]}" in texts
    assert f"ChatGPT custom instructions › how to respond\n\n{CI_NEW[1]}" in texts
    hist = [e for e in out["entries"] if e["origin"].startswith("custom-instructions-history/")]
    assert sorted(e["origin"] for e in hist) == ["custom-instructions-history/2025-02-19/about-you",
                                                 "custom-instructions-history/2025-02-19/how-to-respond"]
    assert all(e["outcome"] == "history" for e in hist)
    assert not any(CI_OLD[0] in t for t in texts)


def test_metadata_only_and_fenced_only_custom_instructions_give_the_same_text(tmp_path):
    meta = msg(content={"content_type": "user_editable_context"},
               metadata={"user_context_message_data": {"about_user_message": CI_OLD[0], "about_model_message": CI_OLD[1]}})
    a = write_folder(tmp_path / "a", [conv("c", T1, [meta])], extra=False)
    b = write_folder(tmp_path / "b", [conv("c", T1, [msg(content=fenced_ci(*CI_OLD))])], extra=False)
    oa, ob = run(a, tmp_path / "a.db"), run(b, tmp_path / "b.db")
    assert view(oa) == view(ob) and oa["counts"]["new"] == 2
    assert [e["text"] for e in oa["entries"]][0].endswith(CI_OLD[0])
    multi = msg(content={"content_type": "user_editable_context",
                         "user_profile": f"x\n{FENCE}first{FENCE}\nmore\n{FENCE}\nthe real one\nacross lines\n{FENCE}",
                         "user_instructions": "no fence at all, so the whole value"})
    c = write_folder(tmp_path / "c", [conv("c", T1, [multi])], extra=False)
    got = run(c, tmp_path / "c.db")
    assert got["entries"][0]["text"].endswith("the real one across lines") and "no fence at all, so the" in got["entries"][1]["text"]


# -- which list is newest ---------------------------------------------------------------------------------------------------

def test_the_newest_list_wins_by_message_time_then_conversation_time_and_a_tie_goes_to_the_later_one(tmp_path):
    early = conv("a", T3, [msg(content=mlist("Old list item.")), ], )                       # conversation time T3 ...
    later = conv("b", T1, [msg(t=T3 + 50, content=mlist("Newer by message time."))])        # ... but this message is later
    out = run(write_folder(tmp_path / "x", [early, later], extra=False), tmp_path / "c.db")
    assert [e["text"] for e in out["entries"] if e["origin"] == "memory#1"] == ["Newer by message time."]
    tie1 = conv("a", T2, [msg(content=mlist("First of a tie."))])
    tie2 = conv("b", T2, [msg(content=mlist("Second of a tie."))])
    out = run(write_folder(tmp_path / "y", [tie1, tie2], extra=False), tmp_path / "d.db")
    assert [e["text"] for e in out["entries"] if e["origin"] == "memory#1"] == ["Second of a tie."]
    no_times = conv("n", None, [msg(content=mlist("Undated list item."))])
    no_times["update_time"] = T2
    out = run(write_folder(tmp_path / "z", [no_times], extra=False), tmp_path / "e.db")
    assert out["counts"]["new"] == 1                                                         # update_time is the last resort


def test_older_list_items_are_listed_once_with_the_newest_older_list_that_had_them(tmp_path):
    l1 = conv("1", T1, [msg(content=mlist("Item X.", "Item Y."))])
    l2 = conv("2", T2, [msg(content=mlist("Item X.", "Item Z."))])
    l3 = conv("3", T3, [msg(content=mlist("Item W."))])
    out = run(write_folder(tmp_path / "x", [l1, l2, l3], extra=False), tmp_path / "c.db")
    hist = [(e["origin"], e["outcome"]) for e in out["entries"] if e["outcome"] == "history"]
    assert hist == [("memory-history/2025-06-15#1", "history"), ("memory-history/2025-06-15#2", "history"),
                    ("memory-history/2025-02-19#2", "history")]                                 # X once (from the newer list), Z, Y


# -- check 4: the same entries from every form ----------------------------------------------------------------------------------

def test_zip_folder_and_split_conversation_files_give_the_same_entries(tmp_path):
    convs = three_conversations() + [conv("c4", T3 - 5, [msg(content={"content_type": "text", "parts": ["just chat"]})])]
    zpath = write_zip(tmp_path / "export.zip", convs)
    folder = write_folder(tmp_path / "folder", convs)
    split = write_folder(tmp_path / "split", convs, split=True)
    results = [run(p, tmp_path / f"{i}.db") for i, p in enumerate((zpath, folder, split))]
    assert all(r["reader"] == "chatgpt-export" for r in results)
    assert view(results[0]) == view(results[1]) == view(results[2])
    assert results[0]["groups"] == results[1]["groups"] == results[2]["groups"]
    nested = tmp_path / "nested"
    write_folder(nested / "export-2026" / "data", convs, extra=False)
    assert view(run(nested, tmp_path / "n.db")) == view(results[0])                           # up to two folders deep


def test_a_zip_is_read_in_place_nothing_is_extracted_or_written(tmp_path):
    zpath = write_zip(tmp_path / "export.zip", three_conversations())
    before = sorted(p.name for p in tmp_path.iterdir()), zpath.stat().st_mtime_ns, zpath.read_bytes()
    run(zpath, tmp_path / "c.db")
    after = sorted(p.name for p in tmp_path.iterdir()), zpath.stat().st_mtime_ns, zpath.read_bytes()
    assert after[1:] == before[1:] and set(after[0]) - set(before[0]) <= {"c.db"}


# -- check 5: --include-removed ------------------------------------------------------------------------------------------------

def test_include_removed_makes_every_history_entry_new_and_other_readers_refuse_it(tmp_path, export):
    base = run(export, tmp_path / "c.db")
    out = run(export, tmp_path / "c.db", options={"include_removed": True}, apply=True, backup=False)
    assert base["counts"]["history"] == 4 and out["counts"]["history"] == 0
    assert out["counts"]["new"] == base["counts"]["new"] + base["counts"]["history"]
    assert [e["origin"] for e in out["entries"]] == [e["origin"] for e in base["entries"]]      # same origins
    texts = stored(tmp_path / "c.db")
    assert A3 in texts and W_OLD in texts and any(CI_OLD[0] in t for t in texts)
    notes_dir = tmp_path / "md"
    notes_dir.mkdir()
    (notes_dir / "a.md").write_text("A note.\n", encoding="utf-8")
    with pytest.raises(import_core.ImportFailed, match="--include-removed is not used by the markdown reader"):
        run(notes_dir, tmp_path / "m.db", options={"include_removed": True})
    code = hermind_cli.main(["import", str(notes_dir), "--include-removed", "--db", str(tmp_path / "m.db")])
    assert code != 0


# -- check 6: files that must stay closed, remarks ---------------------------------------------------------------------------

def test_user_json_with_mode_000_causes_no_error_and_the_remarks_name_it_and_the_rest(tmp_path, export):
    (export / "user.json").chmod(0)
    try:
        out = run(export, tmp_path / "c.db", apply=True, backup=False)
    finally:
        (export / "user.json").chmod(0o644)
    assert out["ok"]
    joined = "\n".join(out["remarks"])
    assert "3 conversations were looked through" in joined and "not memory" in joined
    assert "chat.html" in joined and "user.json (never opened)" in joined and "message_feedback.json" in joined
    assert "3 files in folders of the export were not read (2 .png, 1 .json)" in joined


def test_the_model_editable_context_fields_that_are_not_imported_are_named(tmp_path):
    c = conv("c", T2, [msg(content=mlist(A1, repository="repo text", repo_summary="a summary"))])
    out = run(write_folder(tmp_path / "x", [c], extra=False), tmp_path / "c.db")
    assert any("other fields of the memory list were not imported: repo_summary (1 time), repository (1 time)" in r for r in out["remarks"])
    assert out["counts"]["new"] == 1


# -- check 7: bad input -------------------------------------------------------------------------------------------------------

def test_a_conversation_without_mapping_and_one_with_a_bad_memory_list_are_skipped_and_counted(tmp_path):
    bad_list = conv("badlist", T2, [msg(content={"content_type": "model_editable_context", "model_set_context": ["not", "a", "string"]}),
                                    msg("assistant", {"content_type": "text", "parts": ["Lost with its conversation."]}, t=T2, recipient="bio")])
    no_mapping = {"id": "nomap", "title": "x", "create_time": T2}
    null_mapping = {"id": "nullmap", "mapping": None}
    good = conv("good", T3, [msg(content=mlist(A1))])
    out = run(write_folder(tmp_path / "x", [no_mapping, bad_list, null_mapping, good], extra=False), tmp_path / "c.db", apply=True, backup=False)
    assert out["ok"] and out["counts"]["new"] == 1
    (r,) = [r for r in out["remarks"] if "skipped" in r]
    assert "3 conversations were skipped" in r and "2 with no usable mapping" in r and "1 whose memory list is not a string" in r
    assert "nomap" in r and "badlist" in r
    assert not any("Lost with" in t for t in stored(tmp_path / "c.db"))


def test_a_file_that_is_cut_off_keeps_what_was_read_before(tmp_path):
    folder = write_folder(tmp_path / "x", three_conversations(), extra=False)
    data = (folder / "conversations.json").read_text(encoding="utf-8")
    (folder / "conversations.json").write_text(data[:len(data) - 40], encoding="utf-8")
    out = run(folder, tmp_path / "c.db")
    assert out["ok"] and any("conversations.json" in r and "before that point" in r for r in out["remarks"])
    assert out["counts"]["new"] >= 1


def test_no_memory_list_means_every_write_is_an_ordinary_entry_and_a_remark_says_why(tmp_path):
    c = conv("c", T2, [msg("assistant", {"content_type": "text", "parts": [W_OLD]}, t=T2, recipient="bio"),
                       msg("assistant", {"content_type": "text", "parts": [W_NEW]}, t=T2 + 9, recipient="bio")])
    out = run(write_folder(tmp_path / "x", [c], extra=False), tmp_path / "c.db")
    assert out["counts"]["new"] == 2 and out["counts"]["history"] == 0
    assert any("no memory list" in r and "nothing could be judged removed" in r for r in out["remarks"])


def test_a_conversation_file_is_streamed_not_loaded_whole(tmp_path):
    big = [conv(f"c{i}", T2 + i, [msg(content={"content_type": "text", "parts": ["filler " * 200]})]) for i in range(60)]
    big.append(conv("last", T3, [msg(content=mlist(A1, A2))]))
    folder = write_folder(tmp_path / "x", big, extra=False)
    import io
    raw = (folder / "conversations.json").read_bytes()
    got = list(rx.iter_json_array(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"), step=1000))
    assert len(got) == 61 and got[-1]["id"] == "last"
    assert run(folder, tmp_path / "c.db")["counts"]["new"] == 2


def test_detection(tmp_path, export):
    assert import_core.detect_reader(export) == ("chatgpt-export", [])
    assert import_core.detect_reader(write_zip(tmp_path / "e.zip", three_conversations())) == ("chatgpt-export", [])
    other = tmp_path / "other"
    other.mkdir()
    (other / "conversations.json").write_text('[{"chat_messages": []}]', encoding="utf-8")
    assert rx.detect_chatgpt(other) == 0
    (tmp_path / "plain.zip").write_bytes(b"not a zip")
    assert rx.detect_chatgpt(tmp_path / "plain.zip") == 0


# -- check 8: groups -------------------------------------------------------------------------------------------------------

def test_group_exclusion_changes_only_that_group(tmp_path, export):
    base = run(export, tmp_path / "c.db")
    names = [g["name"] for g in base["groups"]]
    assert names == ["removed or older versions", "memory", "memory writes", "custom instructions"]
    assert [g["exclude"] for g in base["groups"]] == ["*-history/*", "memory#*", "memory-write/*", "custom-instructions/*"]
    for k in import_core.OUTCOMES:
        assert sum(g["counts"][k] for g in base["groups"]) == base["counts"][k]
    for g in base["groups"]:
        out = run(export, tmp_path / "c.db", exclude=[g["exclude"]])
        left_out = {e["origin"] for e in base["entries"] if e["group"] == g["name"]}
        for e0, e1 in zip(base["entries"], out["entries"]):
            if e0["group"] == g["name"]:
                assert e1["outcome"] == "excluded"
            elif e0["outcome"] == "duplicate" and e0.get("of_origin") in left_out:
                assert e1["outcome"] == "new"           # its twin is left out, so it comes in itself
            else:
                assert (e1["outcome"], e1["reason"]) == (e0["outcome"], e0["reason"])
    text = hermind_cli.import_text(base)
    assert "By source:" in text and "--exclude '*-history/*'" in text


def test_apply_twice_gives_only_duplicates_and_a_revert_leaves_no_note(tmp_path, export):
    db = tmp_path / "c.db"
    first = run(export, db, apply=True, backup=False)
    second = run(export, db, apply=True, backup=False)
    assert second["counts"]["new"] == 0 and second["batch"] is None
    import_core.run_revert(first["batch"], db, apply=True, backup=False)
    assert stored(db) == []


def test_a_zip_whose_member_names_start_with_dot_slash_is_read(tmp_path):
    """Some ZIP tools store members as ./conversations.json: the reader lists them without the ./ and still opens them."""
    zipped = write_zip(tmp_path / "plain.zip", three_conversations())
    plain = run(zipped, tmp_path / "a.db")
    dotted = tmp_path / "dotted.zip"
    with zipfile.ZipFile(zipped) as src, zipfile.ZipFile(dotted, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr("./" + info.filename, src.read(info.filename))
    out = run(dotted, tmp_path / "b.db")
    assert out["reader"] == "chatgpt-export"
    assert [(e["origin"], e["outcome"]) for e in out["entries"]] == [(e["origin"], e["outcome"]) for e in plain["entries"]]


def _convs():
    return three_conversations()


def test_zip_members_that_are_links_or_climb_out_are_refused(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(_convs()))
        zf.writestr("../conversations-001.json", json.dumps(_convs()))
        zf.writestr("/etc/conversations-002.json", json.dumps(_convs()))
        zf.writestr("a/../../conversations-003.json", json.dumps(_convs()))
        link = zipfile.ZipInfo("conversations-004.json")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, "/etc/hostname")
    with rx.ExportSource(z) as src:
        assert src.names == ["conversations.json"]
        why = dict(src.refused)
        assert "symbolic link" in why["conversations-004.json"]
        assert sum("unsafe name" in r for r in why.values()) == 3
    out = run(z, tmp_path / "t.db")
    assert any("not read" in r and "unsafe name" in r and "symbolic link" in r for r in out["remarks"])
    assert out["counts"]["new"] == run(write_zip(tmp_path / "ok.zip", _convs()), tmp_path / "u.db")["counts"]["new"]


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_links_in_an_unpacked_export_folder_are_not_followed(tmp_path):
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "conversations-007.json").write_text(json.dumps(_convs()), encoding="utf-8")
    export = write_folder(tmp_path / "export", _convs())
    os.symlink(outside / "conversations-007.json", export / "conversations-007.json")
    os.symlink(outside, export / "more")
    with rx.ExportSource(export) as src:
        assert "conversations-007.json" not in src.names and not any(n.startswith("more/") for n in src.names)
        assert sorted(n for n, _ in src.refused) == ["conversations-007.json", "more/"]
    out = run(export, tmp_path / "t.db")
    assert any("symbolic link" in r for r in out["remarks"])


def test_zip_member_names_are_normalised_before_the_unsafe_check(tmp_path):
    """Round 2: ".//etc/x.json" was kept as "/etc/x.json" (the check ran before "./" was stripped)."""
    z = tmp_path / "dots.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(_convs()))
        zf.writestr(".//etc/conversations-001.json", json.dumps(_convs()))
        zf.writestr("././/etc/conversations-002.json", json.dumps(_convs()))
        zf.writestr("./../conversations-003.json", json.dumps(_convs()))
        zf.writestr(".\\..\\conversations-004.json", json.dumps(_convs()))
        zf.writestr("./conversations-005.json", json.dumps(_convs()))          # a plain ./ name is still fine
    with rx.ExportSource(z) as src:
        assert src.names == ["conversations-005.json", "conversations.json"]
        assert sorted(n for n, _ in src.refused) == sorted([".//etc/conversations-001.json", "././/etc/conversations-002.json",
                                                             "./../conversations-003.json", ".\\..\\conversations-004.json"])
        assert all("unsafe name" in why for _, why in src.refused)
