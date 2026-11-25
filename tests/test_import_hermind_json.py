"""SPEC-4 M4: `hermind import` reads Hermind's own JSON export back (the hermind-json reader), old versions included."""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_fixture import DAY, build_db, table_dump  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"
sys.path.insert(0, str(ROOT))
import export_notes  # noqa: E402
import hermind_cli  # noqa: E402
import import_core  # noqa: E402
import reader_hermind_json  # noqa: E402
from store import DecisionStore  # noqa: E402

NOW = time.time()
NINE = ("notes", "postings", "note_entities", "note_links", "vectors", "tombstones", "learn_pending", "entities", "entity_links")

# fields of the round trip that are compared, and the ones that are left out (each with its reason): see the test below
SAME = ("content", "kind", "keywords", "pinned", "trust", "scope", "review", "status", "valid_from", "valid_to", "created_at",
        "event_at", "proof_count", "gen_keywords", "learned_terms")
LEFT_OUT = {
    "id": "B numbers its own notes (the ids are mapped instead)",
    "expires_at": "imported notes never expire",
    "source_session": "every imported note says import:hermind",
    "content_hash": "derived from the text (equal for live notes, empty for older versions in both)",
    "doc_len_content": "derived from the text", "doc_len_keywords": "derived from the text",
    "consolidated_at": "bookkeeping of the consolidation that ran in A",
    "locked": "bookkeeping of the consolidation that ran in A",
    "candidate": "a capture-time flag, not carried",
    "access_count": "a counter of use: SPEC-3's idle clock starts at the import",
    "last_access": "a counter of use: SPEC-3's idle clock starts at the import",
    "shown_count": "a counter of use", "opened_count": "a counter of use", "fb_at": "a counter of use",
    "gen_at": "carried, but the time of generation is not a field of the round trip",
    "gen_model": "carried, but the model name is not a field of the round trip",
    "canonical_id": "compared after mapping the ids, see the links check",
    "project": "never exported (a path of the writer's machine); an imported note has none",
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def world(tmp_path):
    """Database A (the fixture, written 40 days ago, with learned fields on one note), its export, and an empty B."""
    a = tmp_path / "a.db"
    t0 = NOW - 40 * DAY
    ids = build_db(a, t0=t0)
    s = DecisionStore(str(a))
    try:
        with s.conn:
            s.set_gen_keywords(ids["edited"], ["dashboard sqlite", "panel storage"], "test-model", now=t0 + DAY)
            s._set_learned(ids["rich"], ["retention", "ledger"])
            s.conn.execute("UPDATE notes SET access_count=7, last_access=?, shown_count=3 WHERE id=?", (NOW, ids["pinned"]))
        ids["dated"], _ = s.add("The audit review meeting is on 2026-03-05.", kind="decision", created_at=t0 + 60 * DAY)
    finally:
        s.close()
    exported = tmp_path / "a.json"
    export_json(a, exported)
    return {"a": a, "b": tmp_path / "b.db", "json": exported, "ids": ids, "t0": t0, "tmp": tmp_path}


def export_json(db, out, **kw):
    s = DecisionStore(str(db))
    try:
        res = export_notes.run_export(s, fmt="json", out=str(out), force=True, **kw)
    finally:
        s.close()
    return res


def records(db):
    s = DecisionStore(str(db))
    try:
        return {r["id"]: r for r in export_notes.collect_records(s.conn)}
    finally:
        s.close()


def do_import(world, source=None, **kw):
    kw.setdefault("apply", True)
    kw.setdefault("backup", False)
    return import_core.run_import(str(source or world["json"]), world["b"], **kw)


def id_map(db, batch=None):
    """A's id -> B's id, from the batch record (origins are note/<id> and version/<id>)."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT origin, note_id FROM import_batch_notes" + (" WHERE batch_id=?" if batch else ""),
                           (batch,) if batch else ()).fetchall()
    finally:
        con.close()
    return {int(o.split("/")[1]): n for o, n in rows}


def entities_of(rec):
    return sorted((e["name"], tuple(sorted(e["aliases"]))) for e in rec["entities"])


@pytest.mark.parametrize("nested", [False, True])
def test_imported_learned_metadata_keeps_only_public_text(tmp_path, nested):
    source, db, output = (tmp_path / f for f in ("source.json", "target.db", "again.json"))
    note = {"id": 1, "kind": "decision", "status": "active", "content": "A fictional pottery catalogue.",
            "gen_keywords": "glazecard <private>mauvecanary</private>",
            "learned_terms": "claywheel <private>peachcanary</private>",
            "gen_model": "stub <private>moss canary</private>"}
    if nested:
        for field, term in (("gen_keywords", "mauvecanary"), ("learned_terms", "peachcanary")):
            note[field] = note[field].replace(term, f"<private>inner</private> {term}")
    source.write_text(json.dumps([note]), encoding="utf-8")
    assert do_import({"json": source, "b": db})["created"] == 1
    export_json(db, output)
    exported = output.read_text(encoding="utf-8")
    row, = json.loads(exported)
    assert (row["gen_keywords"], row["learned_terms"], row["gen_model"]) == ("glazecard", "claywheel", "stub")
    from contextlib import closing
    with closing(DecisionStore(str(db))) as store:
        dump = "\n".join(store.conn.iterdump())
        for word in ("glazecard", "claywheel"):
            assert row["id"] in {n["id"] for n in store.search(word)}
        for word in ("mauvecanary", "peachcanary", "moss canary"):
            assert word not in dump and word not in exported
            assert row["id"] not in {n["id"] for n in store.search(word)}


@pytest.mark.parametrize(("model", "expected"), [
    ("Kiln/Blue  2.1", "Kiln/Blue  2.1"),
    ("Kiln <private>draft <private>clay</private> sample</private> Blue", "Kiln Blue"),
    ("<private>draft <private>clay</private> sample</private>", ""),
])
def test_imported_model_label_round_trip(tmp_path, model, expected):
    source, db, output = (tmp_path / f for f in ("kiln.json", "target.db", "again.json"))
    note = {"id": 1, "kind": "tech", "status": "active", "content": "Catalogue of ceramic tiles.",
            "gen_model": model}
    source.write_text(json.dumps([note]), encoding="utf-8")
    assert do_import({"json": source, "b": db})["created"] == 1
    from contextlib import closing
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT gen_model FROM notes").fetchall() == [(expected,)]
    export_json(db, output)
    row, = json.loads(output.read_text(encoding="utf-8"))
    assert row["gen_model"] == expected


def test_private_import_labels_round_trip(tmp_path):
    source, db, output = (tmp_path / f for f in ("source.json", "target.db", "again.json"))
    row = {"id": 1, "status": "active", "kind": "decision", "content": "Fictional glaze samples are ready.",
           "tags": "catalog,<private>quiet jade,plum</private> fired",
           "entities": [{"name": "Kiln=East <private>hidden kiln</private>", "aliases": ["kiln card"]},
                        {"name": "<private>hidden rack</private>"}]}
    source.write_text(json.dumps([row]), encoding="utf-8")
    result = do_import({"json": source, "b": db})
    assert result["ok"] and result["created"] == 1
    export_json(db, output)
    exported = output.read_text(encoding="utf-8")
    note, = json.loads(exported)
    assert {"catalog", "fired"} <= set(note["tags"].split())
    assert entities_of(note) == [("Kiln=East", ("kiln card",))]
    con = sqlite3.connect(db)
    try:
        persisted = "\n".join(con.iterdump())
    finally:
        con.close()
    for surface in (exported, persisted):
        assert not any(word in surface for word in ("jade", "plum", "hidden kiln", "hidden rack", "<private>"))


def test_six_json_fields_are_safe_in_preview_apply_storage_and_reexport(tmp_path):
    source, db, output = (tmp_path / name for name in ("pottery.json", "target.db", "again.json"))
    target = {"json": source, "b": db}
    shared = "The fictional pottery catalogue lists twelve cups."
    canary = "pottery-canary-"

    def private(name):
        return f"<private>{canary}{name}</private>"

    def note(nid, content, **overrides):
        return {"id": nid, "content": content, "kind": "chat", "status": "active", **overrides}

    data = [
        note(1, shared, scope=f"Pottery:East {private('mixed-scope')}",
             kind=private("kind"), created_at=private("created"), trust=private("trust"),
             links=[{"to": 4, "rel": "References/Glaze  Index"},
                    {"to": 4, "rel": f"compares {private('mixed-rel')}"},
                    {"to": 4, "rel": "compares"}, {"to": 4, "rel": private("rel")}]),
        note(2, shared, scope="Pottery:East", trust=canary + "duplicate-trust"),
        note(3, shared, scope=f"<private>{private('nested-scope')}{canary}default-collision</private>"),
        note(4, "The display includes stoneware.", scope="Glaze:@North", kind=" TeCh ",
             created_at="2021-01-01T00:00:00Z", trust="0.75"),
        note(5, "The display once included terracotta.", status="superseded", scope=f"Glaze:@North {private('old-scope')}",
             kind="state", created_at=1600000000, trust=0.625,
             links=[{"to": 4, "rel": f"superseded_by {private('old-mixed-rel')}"}]),
        note(6, "The display once included porcelain.", status="superseded", canonical_id=4, scope=private("old-scope-only")),
        note(7, "The display once included tin.", status="rejected", scope=private("rejected-scope"),
             kind=[private("kind-list")], created_at=False, trust={"value": private("trust-object")}),
        note(8, "The display once included wax.", status=private("status")),
        note(9, "The display once included earthenware.", status="merged", canonical_id=4,
             links=[{"to": 4, "rel": f"<private>{private('nested-rel')}{canary}canonical-rel</private>"}]),
        note(10, "The display once included paper.", status="superseded",
             links=[{"to": 4, "rel": private("no-canonical-rel")}]),
        note(11, "A fictional vessel that must not be imported.", scope=private("unique-scope")),
        note(12, "A past catalogue draft lists ten cups.", status="merged", canonical_id=3),
        note(13, "The display once included clay beads.", status="superseded", canonical_id=4),
    ]
    source.write_text(json.dumps(data), encoding="utf-8")

    store = DecisionStore(str(db))
    try:
        existing, _ = store.add(shared, kind="preference", scope="default")
    finally:
        store.close()
    before = db.read_bytes()
    preview = do_import(target, apply=False)
    assert preview["batch"] is None and preview["created"] == 0 and db.read_bytes() == before
    applied = do_import(target)
    assert applied["batch"] and applied["created"] == 5

    for report in (preview, applied):
        assert report["ok"] and report["reader"] == "hermind-json"
        assert canary not in json.dumps(report)
        by_origin = {e["origin"]: e for e in report["entries"]}
        assert {o: e["outcome"] for o, e in by_origin.items()} == {
            "note/1": "new", "note/2": "duplicate", "note/3": "refused", "note/4": "new",
            "version/5": "version", "version/6": "history", "rejected/7": "history", "other/8": "history",
            "version/9": "version", "version/10": "history", "note/11": "refused",
            "version/12": "history", "version/13": "version",
        }
        assert by_origin["note/2"]["of_origin"] == "note/1"
        assert "trust invalid" in by_origin["note/2"]["reason"]
        for origin in ("note/1", "rejected/7"):
            reason = by_origin[origin]["reason"]
            assert "kind" in reason and "chosen from the text" in reason
            assert "created_at" in reason and "import time" in reason
            assert "trust" in reason and "default" in reason
        assert by_origin["note/4"]["reason"] == ""
        assert "rejected in review" in by_origin["rejected/7"]["reason"]
        assert "older version" in by_origin["version/6"]["reason"]
        for origin in ("note/3", "note/11", "version/6", "rejected/7"):
            assert "scope" in by_origin[origin]["reason"] and "not imported" in by_origin[origin]["reason"]
        assert "status" in by_origin["other/8"]["reason"] and "not imported" in by_origin["other/8"]["reason"]
        assert "no note it belongs to" in by_origin["version/10"]["reason"]
        assert "note/3" in by_origin["version/12"]["reason"] and "scope" in by_origin["version/12"]["reason"]
        assert any(r.startswith("3 of 3 link(s)") for r in report["remarks"])

    m = id_map(db, applied["batch"])
    assert set(m) == {1, 4, 5, 9, 13}
    expected_links = {(m[5], m[4], "superseded_by"), (m[13], m[4], "version_of")}
    con = sqlite3.connect(db)
    try:
        assert set(con.execute("SELECT src, dst, rel FROM note_links")) == expected_links
        fields = ("scope", "kind", "created_at", "trust", "status", "canonical_id")
        rows = {r[0]: r[1:] for r in con.execute("SELECT id, " + ", ".join(fields) + " FROM notes")}
        persisted = "\n".join(con.iterdump())
    finally:
        con.close()
    assert set(rows) == {existing, *m.values()}
    assert rows[existing][:2] == ("default", "preference")
    assert rows[m[1]][:2] == ("Pottery:East", "chat") and rows[m[1]][2] >= NOW
    assert rows[m[1]][3:] == (0.5, "active", None)
    assert rows[m[4]] == ("Glaze:@North", "tech", 1609459200, 0.75, "active", None)
    assert rows[m[5]] == ("Glaze:@North", "state", 1600000000, 0.625, "superseded", m[4])
    assert rows[m[9]][4:] == ("merged", m[4])
    assert canary not in persisted and "<private>" not in persisted

    export_json(db, output)
    exported = output.read_text(encoding="utf-8")
    assert canary not in exported and "<private>" not in exported
    again = {r["id"]: r for r in json.loads(exported)}
    assert {nid: tuple(r[f] for f in fields) for nid, r in again.items()} == rows
    assert {(nid, link["to"], link["rel"]) for nid, r in again.items() for link in r["links"]} == expected_links


@pytest.mark.parametrize(("metadata", "origin", "outcome", "field"), [
    ({"kind": ""}, "note/1", "new", "kind"),
    ({"created_at": " "}, "note/1", "new", "created_at"),
    ({"created_at": float("inf")}, "note/1", "new", "created_at"),
    ({"trust": True}, "note/1", "new", "trust"),
    ({"trust": 10**400}, "note/1", "new", "trust"),
    ({"status": None}, "note/1", "new", "status"),
    ({"status": ["active", "fictional-status-canary"]}, "other/1", "history", "status"),
])
def test_explicit_invalid_json_metadata_is_explained_without_changing_outcomes(tmp_path, metadata, origin, outcome, field):
    source, db = tmp_path / "pottery.json", tmp_path / "target.db"
    source.write_text(json.dumps([{"id": 1, "kind": "chat", "status": "active",
                                   "content": "A fictional pottery notice.", **metadata}]), encoding="utf-8")
    report = do_import({"json": source, "b": db}, apply=False)
    assert report["ok"] and not db.exists()
    entry, = report["entries"]
    assert (entry["origin"], entry["outcome"]) == (origin, outcome)
    assert field in entry["reason"] and "fictional-status-canary" not in json.dumps(report)


def test_the_round_trip_keeps_every_live_note_and_older_version(world):
    out = do_import(world)
    assert out["ok"] and out["reader"] == "hermind-json" and out["reader_detected"]
    assert out["counts"]["new"] == 10 and out["counts"]["version"] == 3 and out["counts"]["history"] == 1   # 1 rejected
    a, b = records(world["a"]), records(world["b"])
    m = id_map(world["b"], out["batch"])
    kept = {i: r for i, r in a.items() if r["status"] != "rejected"}
    assert set(m) == set(kept) and len(b) == len(kept) == 13 - 1 + 1
    for aid, ra in kept.items():
        rb = b[m[aid]]
        for f in SAME:
            assert rb[f] == ra[f] and type(rb[f]) is type(ra[f]), (aid, f, ra[f], rb[f])
        tags_b = [t for t in rb["tags"].split() if not t.startswith("import-")]
        assert tags_b == ra["tags"].split(), aid
        assert entities_of(rb) == entities_of(ra), (aid, entities_of(ra), entities_of(rb))
        assert sorted((m[l["to"]], l["rel"]) for l in ra["links"]) == sorted((l["to"], l["rel"]) for l in rb["links"]), aid
        assert (m.get(ra["canonical_id"]) if ra["canonical_id"] else None) == rb["canonical_id"], aid
    # the counters of use and the idle clock are not carried
    pinned = b[m[world["ids"]["pinned"]]]
    assert pinned["access_count"] == 0 and pinned["shown_count"] == 0 and pinned["last_access"] is not None
    assert all(b[m[i]]["source_session"] == "import:hermind" and b[m[i]]["expires_at"] is None for i in kept)
    assert all(r["tags"].split()[-1].startswith("import-") for r in b.values())


def test_the_fields_the_round_trip_leaves_out_are_all_named(world):
    cols = {r[1] for r in sqlite3.connect(world["a"]).execute("PRAGMA table_info(notes)")}
    assert cols == set(SAME) | set(LEFT_OUT) | {"tags"}, cols ^ (set(SAME) | set(LEFT_OUT) | {"tags"})


def test_the_same_search_finds_the_same_notes_in_the_same_order_in_a_and_b(world):
    do_import(world)
    m = id_map(world["b"])
    t0 = world["t0"]
    day = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts))
    queries = [
        ("orchid dashboard database", {}), ("nebula backups marlin rack", {}), ("zephyr gateway port", {}),
        ("falcon server ram", {}), ("audit reports retention", {}),
        ("orchid dashboard postgresql", {"include_history": True}), ("nebula backups nightly", {"include_history": True}),
        ("zephyr gateway port 8080", {"include_history": True}),
        ("orchid dashboard postgresql", {"as_of": day(t0 + 2 * DAY)}), ("zephyr gateway port", {"as_of": day(t0 + 12 * DAY)}),
    ]
    sa, sb = DecisionStore(str(world["a"])), DecisionStore(str(world["b"]))
    try:
        for q, kw in queries:
            ha = [h["id"] for h in sa.search(q, limit=8, **kw)]
            hb = [h["id"] for h in sb.search(q, limit=8, **kw)]
            assert ha, (q, kw)
            assert [m[i] for i in ha] == hb, (q, kw, ha, hb)
        # an older version is history: found with include_history or as_of, never by a plain search
        assert 2 in [h["id"] for h in sa.search("orchid dashboard postgresql", limit=8, include_history=True)]
        assert m[2] not in [h["id"] for h in sb.search("orchid dashboard postgresql", limit=8)]
        assert m[2] in [h["id"] for h in sb.search("orchid dashboard postgresql", limit=8, as_of=day(t0 + 2 * DAY))]
    finally:
        sa.close(); sb.close()


def test_importing_the_same_export_a_second_time_writes_nothing(world):
    first = do_import(world)
    before = table_dump(world["b"])
    out = do_import(world)
    assert out["ok"] and out["batch"] is None and out["created"] == 0
    assert out["counts"]["new"] == 0 and out["counts"]["version"] == 0
    assert out["counts"]["duplicate"] == 10 and out["counts"]["history"] == 4         # 3 older versions + 1 rejected
    by_origin = {e["origin"]: e for e in out["entries"]}
    assert all(e["outcome"] == "duplicate" for o, e in by_origin.items() if o.startswith("note/"))
    for o in ("version/2", "version/4", "version/5"):
        assert by_origin[o]["outcome"] == "history" and "duplicate of note #" in by_origin[o]["reason"], by_origin[o]
    assert by_origin["rejected/9"]["reason"] == "rejected in review"
    assert table_dump(world["b"]) == before and first["batch"]


def test_a_revert_leaves_b_with_no_note_and_no_alias_the_import_added(world):
    DecisionStore(str(world["b"])).close()
    empty = table_dump(world["b"])
    out = do_import(world)
    assert out["created"] == 13
    r = import_core.run_revert(out["batch"], world["b"], apply=True, backup=False)
    assert len(r["remove"]) == 13 and r["keep"] == [] and r["gone"] == []
    con = sqlite3.connect(world["b"])
    assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0                # no note in any status
    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    con.close()
    after = table_dump(world["b"])
    assert {t: after[t] for t in NINE} == {t: empty[t] for t in NINE}
    assert sqlite3.connect(world["b"]).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # and the same export comes in again
    again = do_import(world)
    assert again["counts"]["new"] == 10 and again["counts"]["version"] == 3


def test_a_revert_takes_back_the_aliases_the_import_added_to_an_entity_that_was_already_there(world):
    s = DecisionStore(str(world["b"]))
    s.add("Priya presented the Orchid roadmap to the board.", kind="chat", created_at=NOW)
    s.close()
    before = {r[0]: r[1] for r in sqlite3.connect(world["b"]).execute("SELECT key, aliases FROM entities")}
    assert before["orchid"] == ""
    out = do_import(world)
    now = {r[0]: r[1] for r in sqlite3.connect(world["b"]).execute("SELECT key, aliases FROM entities")}
    assert now["orchid"] == "orchid dashboard|ORC"
    import_core.run_revert(out["batch"], world["b"], apply=True, backup=False)
    after = {r[0]: r[1] for r in sqlite3.connect(world["b"]).execute("SELECT key, aliases FROM entities")}
    assert after == before                                                              # the entity stays, its aliases go


def test_an_older_version_whose_live_note_is_excluded_is_history_with_that_reason(world):
    out = do_import(world, apply=False, exclude=["note/1"])
    e = {x["origin"]: x for x in out["entries"]}
    assert e["note/1"]["outcome"] == "excluded"
    assert e["version/2"]["outcome"] == "history"
    assert "note/1" in e["version/2"]["reason"] and "excluded" in e["version/2"]["reason"]
    assert e["version/4"]["outcome"] == "version"                                       # the others are not touched
    # excluding the group of older versions excludes exactly them
    out = do_import(world, apply=False, exclude=["version/*"])
    assert out["counts"]["excluded"] == 3 and out["counts"]["version"] == 0 and out["counts"]["new"] == 10


def test_an_older_version_of_a_duplicate_or_refused_note_says_which_note_and_why(world):
    s = DecisionStore(str(world["b"]))
    nid, _ = s.add("Priya keeps the Orchid dashboard on SQLite since March.", kind="tech", created_at=NOW)   # = note 1's text
    s.add("Always answer in British English.", kind="preference", created_at=NOW)
    s.close()
    data = json.loads(world["json"].read_text())
    for rec in data:
        if rec["id"] == 3:                                                              # a credential-like live note
            rec["content"] = "deploy key is " + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    broken = world["tmp"] / "edited.json"
    broken.write_text(json.dumps(data))
    out = do_import(world, source=broken, apply=False)
    e = {x["origin"]: x for x in out["entries"]}
    assert e["note/1"]["outcome"] == "duplicate" and e["note/1"]["note_id"] == nid
    assert e["version/2"]["outcome"] == "history"
    assert e["version/2"]["reason"] == f"an older version of note/1, which is a duplicate of note #{nid}"
    assert e["note/3"]["outcome"] == "refused"
    assert e["version/4"]["outcome"] == "history" and "note/3" in e["version/4"]["reason"] and "refused" in e["version/4"]["reason"]
    assert out["counts"]["version"] == 1                                                # only version/5 still has its note new


def test_an_older_version_is_judged_by_its_own_text_rules_but_never_as_a_duplicate_of_its_note(world):
    data = json.loads(world["json"].read_text())
    for rec in data:
        if rec["id"] == 2:                                                              # the old text now looks like a secret
            rec["content"] = "old token " + "s" + "k_live_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
        if rec["id"] == 4:                                                              # the old text equals the live text
            rec["content"] = next(r["content"] for r in data if r["id"] == 3)
    p = world["tmp"] / "edited.json"
    p.write_text(json.dumps(data))
    e = {x["origin"]: x for x in do_import(world, source=p, apply=False)["entries"]}
    assert e["version/2"]["outcome"] == "refused" and e["version/2"]["reason"].startswith("refused:")
    assert e["version/4"]["outcome"] == "version"
    out = do_import(world, source=p)
    con = sqlite3.connect(world["b"])
    texts = [r[0] for r in con.execute("SELECT content FROM notes WHERE status='merged'")]
    assert texts == ["Nebula backups for the Marlin rack run every night at two."]


def test_an_older_version_whose_note_is_not_in_the_file_is_history(world):
    data = [r for r in json.loads(world["json"].read_text()) if r["id"] != 6]           # note 6 is the home of version 5
    p = world["tmp"] / "partial.json"
    p.write_text(json.dumps(data))
    e = {x["origin"]: x for x in do_import(world, source=p, apply=False)["entries"]}
    assert e["version/5"]["outcome"] == "history" and "#6" in e["version/5"]["reason"]


def test_the_old_five_key_export_imports_its_live_notes_with_from(world):
    old = [dict(zip(("id", "kind", "content", "keywords", "created_at"), tuple(r))) for r in sqlite3.connect(world["a"]).execute(
        "SELECT id, kind, content, keywords, created_at FROM notes WHERE status = 'active' ORDER BY id")]
    p = world["tmp"] / "old.json"
    p.write_text(json.dumps(old, indent=2))
    with pytest.raises(import_core.ImportFailed, match="no reader recognises"):
        do_import(world, source=p, apply=False)                                          # not detected: it has no status
    out = do_import(world, source=p, reader="hermind-json")
    assert out["ok"] and out["counts"]["new"] == len(old) == 10 and out["counts"]["version"] == 0
    assert any("first export format" in r for r in out["remarks"])
    b = records(world["b"])
    assert sorted(r["content"] for r in b.values()) == sorted(r["content"] for r in old)
    assert {r["created_at"] for r in b.values()} == {r["created_at"] for r in old}


def test_detection(tmp_path):
    det = reader_hermind_json.detect_hermind_json
    full = tmp_path / "full.json"
    full.write_text(json.dumps([{"id": 1, "kind": "chat", "content": "x", "status": "active"}]))
    assert det(full) == 4
    long_tail = tmp_path / "long_tail.json"                       # the first note goes on past 64 KiB, after its status
    long_tail.write_text(json.dumps([{"id": 1, "kind": "chat", "content": "x", "status": "active", "learned_terms": "word " * 30000}], indent=2))
    assert long_tail.stat().st_size > 64 * 1024 and det(long_tail) == 4
    huge = tmp_path / "huge.json"          # the status of the first note lies beyond 64 KiB: it cannot be seen, so --from is needed
    huge.write_text(json.dumps([{"id": 1, "kind": "chat", "content": "word " * 30000, "status": "active"}], indent=2))
    assert huge.stat().st_size > 64 * 1024 and det(huge) == 0
    cases = {"five.json": [{"id": 1, "kind": "chat", "content": "x", "keywords": "", "created_at": 1.0}],
             "nostatus.json": [{"id": 1, "content": "x", "kind": "chat"}], "objects.json": {"id": 1, "content": "x", "kind": "a", "status": "active"},
             "strings.json": ["a", "b"], "empty.json": []}
    for name, value in cases.items():
        (tmp_path / name).write_text(json.dumps(value))
        assert det(tmp_path / name) == 0, name
    (tmp_path / "x.txt").write_text(full.read_text())
    assert det(tmp_path / "x.txt") == 0 and det(tmp_path) == 0
    assert "hermind-json" in {r["name"] for r in import_core.reader_list()}


def test_origins_and_groups(world):
    out = do_import(world, apply=False)
    assert [g["name"] for g in out["groups"]] == ["notes", "older versions", "rejected"]
    assert [g["exclude"] for g in out["groups"]] == ["note/*", "version/*", "rejected/*"]
    assert [g["total"] for g in out["groups"]] == [10, 3, 1] and sum(g["total"] for g in out["groups"]) == out["total"]
    origins = {e["origin"] for e in out["entries"]}
    assert origins == {f"note/{i}" for i in (1, 3, 6, 7, 8, 10, 11, 12, 13, 14)} | {"version/2", "version/4", "version/5", "rejected/9"}
    for pattern, outcome in (("note/*", "new"), ("version/*", "version"), ("rejected/*", "history")):
        ex = do_import(world, apply=False, exclude=[pattern])
        gone = [e for e in ex["entries"] if e["outcome"] == "excluded"]
        assert len(gone) == next(g["total"] for g in out["groups"] if g["exclude"] == pattern)


def test_links_between_written_notes_are_recreated_and_the_others_are_counted_in_one_remark(world):
    out = do_import(world, apply=False)
    remark = [r for r in out["remarks"] if "link(s)" in r]
    assert remark == ["4 of 4 link(s) between notes are recreated (both notes are written by this import)"]
    s = DecisionStore(str(world["b"]))
    s.add("Priya keeps the Orchid dashboard on SQLite since March.", kind="tech", created_at=NOW)
    s.close()
    out = do_import(world, apply=False)
    remark = [r for r in out["remarks"] if "link(s)" in r]
    assert len(remark) == 1 and remark[0].startswith("3 of 4 link(s)") and "1 is not" in remark[0]


def test_a_note_of_the_batch_that_was_edited_stays_with_its_older_versions(world):
    out = do_import(world)
    m = id_map(world["b"], out["batch"])
    s = DecisionStore(str(world["b"]))
    s.update(m[1], "Priya keeps the Orchid dashboard on DuckDB now.")
    s.close()
    r = import_core.run_revert(out["batch"], world["b"], apply=True, backup=False)
    kept = {k["note_id"]: k["reason"] for k in r["keep"]}
    assert m[1] in kept and "edited after the import" in kept[m[1]]
    assert m[2] in kept and "stays" in kept[m[2]]                                         # its old version stays with it
    con = sqlite3.connect(world["b"])
    ids = {x[0] for x in con.execute("SELECT id FROM notes")}
    assert m[1] in ids and m[2] in ids and m[4] not in ids and m[3] not in ids


def test_a_pinned_or_changed_note_is_not_mistaken_for_one_with_links(world):
    # the links the batch wrote itself are not "new links after the import"
    out = do_import(world)
    plan = import_core.run_revert(out["batch"], world["b"], apply=False)
    assert len(plan["remove"]) == 13 and not plan["keep"]


def test_every_entitys_df_equals_its_number_of_note_entities_rows_after_import_and_revert(world):
    def check(db):
        con = sqlite3.connect(db)
        bad = con.execute("SELECT e.key, e.df, (SELECT COUNT(*) FROM note_entities n WHERE n.entity_id = e.id) "
                          "FROM entities e WHERE e.df != (SELECT COUNT(*) FROM note_entities n WHERE n.entity_id = e.id)").fetchall()
        con.close()
        return bad
    out = do_import(world)
    assert check(world["b"]) == []
    import_core.run_revert(out["batch"], world["b"], apply=True, backup=False)
    assert check(world["b"]) == []


def test_the_command_line_end_to_end(world, capsys):
    argv = ["import", str(world["json"]), "--db", str(world["b"])]
    assert hermind_cli.main(argv) == 0
    text = capsys.readouterr().out
    assert "reader: hermind-json" in text and "3 version" in text and "will import" in text
    assert hermind_cli.main([*argv, "--apply", "--no-backup", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["counts"]["version"] == 3 and d["created"] == 13
    assert hermind_cli.main(["import", "--list", "--db", str(world["b"])]) == 0
    assert "3 version" in capsys.readouterr().out
    assert hermind_cli.main(["import", "--revert", d["batch"], "--apply", "--no-backup", "--db", str(world["b"])]) == 0
    capsys.readouterr()
    assert sqlite3.connect(world["b"]).execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_an_apply_that_fails_while_writing_older_versions_is_incomplete_and_the_revert_removes_it_all(world, monkeypatch):
    real = DecisionStore.add_older_version
    calls = []

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk went away")
        return real(self, *a, **kw)

    monkeypatch.setattr(DecisionStore, "add_older_version", flaky)
    out = do_import(world, chunk=4)
    assert not out["ok"] and "incomplete" in out["error"] and out["batch"]
    batches = import_core.run_list(world["b"])["batches"]
    assert batches[0]["state"] == "incomplete" and batches[0]["notes_created"] >= 8
    monkeypatch.undo()
    r = import_core.run_revert(out["batch"], world["b"], apply=True, backup=False)
    assert r["keep"] == []
    con = sqlite3.connect(world["b"])
    assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM note_links").fetchone()[0] == 0


def test_an_entity_whose_name_contains_an_equals_sign_comes_back(tmp_path):
    """Entity names such as `provider=local` (a setting named in a note) are real names: the round trip keeps them."""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    s = DecisionStore(str(a))
    try:
        nid, _ = s.add("The sync job reads provider=local from its config file.", kind="tech",
                       entities=["provider=local", "sync job"])
        with s.conn:      # the "=" split of `entities` would read provider=local as a name with an alias: set it directly
            eid = s._get_or_create_entity("provider=local", "provider=local", [], seed=False)
            s.conn.execute("INSERT OR IGNORE INTO note_entities(note_id, entity_id) VALUES (?,?)", (nid, eid))
        want = entities_of(records(a)[nid])
    finally:
        s.close()
    assert ("provider=local", ()) in want
    export_json(a, tmp_path / "a.json")
    out = import_core.run_import(str(tmp_path / "a.json"), b, apply=True, backup=False)
    assert out["ok"] and out["counts"]["new"] == 1
    (rec,) = [r for r in records(b).values()]
    assert entities_of(rec) == want


def test_a_note_merged_into_a_note_that_was_merged_in_turn_comes_back_with_only_its_own_link(tmp_path):
    """x merged into y, then y into z: x's canonical is z but its link points at y. The round trip keeps exactly that
    (no second merged_into link from x to z), and a revert still removes every note."""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    t0 = NOW - 40 * DAY
    s = DecisionStore(str(a))
    try:
        x, _ = s.add("Nebula backups for the Marlin rack run nightly at two.", kind="tech", created_at=t0)
        y, _ = s.add("Nebula backups for the Marlin rack run every night at two.", kind="tech", created_at=t0 + DAY)
        z, _ = s.add("Nebula backups for the Marlin rack run each night at two o'clock.", kind="tech", created_at=t0 + 2 * DAY)
        assert s.fold_notes(x, y, "merged", now=t0 + 3 * DAY)
        assert s.fold_notes(y, z, "merged", now=t0 + 4 * DAY)
    finally:
        s.close()
    ra = records(a)
    assert ra[x]["canonical_id"] == z and ra[x]["links"] == [{"to": y, "rel": "merged_into"}]
    export_json(a, tmp_path / "a.json")
    out = import_core.run_import(str(tmp_path / "a.json"), b, apply=True, backup=False)
    assert out["ok"] and out["counts"]["new"] == 1 and out["counts"]["version"] == 2
    m, rb = id_map(b, out["batch"]), records(b)
    for aid, rec in ra.items():
        assert sorted((m[l["to"]], l["rel"]) for l in rec["links"]) == sorted((l["to"], l["rel"]) for l in rb[m[aid]]["links"]), aid
        assert (m.get(rec["canonical_id"]) if rec["canonical_id"] else None) == rb[m[aid]]["canonical_id"], aid
    r = import_core.run_revert(out["batch"], b, apply=True, backup=False)
    assert r["ok"] and not records(b)
