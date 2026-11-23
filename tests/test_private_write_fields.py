"""Positive data-minimisation checks for new writes, using only invented data."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.parametrize("subject,labels,alias,closing_tag,opening_tag", [
    ("Ternarium", "maritime tidal charter", "harbour ledger", "</private>", "<private>"),
    ("Fernorium", "canopy woodland survey", "grove atlas", "＜ /PRIVATE ＞", "＜ PrIvAtE ＞"),
], ids=["maritime", "woodland"])
def test_new_writes_keep_only_public_metadata(mcp_spawn, store_mod, tmp_path, subject, labels, alias,
                                              closing_tag, opening_tag):
    from hermind.screen import strip_private_label

    first, second, edited = labels.split()
    markers = {field: f"fiction{subject.lower()}{field}" for field in ("tag", "alias", "keyword", "edit", "empty")}

    def span(field):
        return opening_tag + markers[field] + closing_tag

    assert strip_private_label(f"{first} [REDACTED] {span('tag')} {second}") == f"{first} [REDACTED] {second}"

    def assert_minimised(surface, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        assert not any(marker in rendered.lower() for marker in markers.values()), surface
        assert "<private>" not in rendered.lower() and "＜ private ＞" not in rendered.lower(), surface

    def sql(query, *args):
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
            return con.execute(query, args).fetchall()

    def notes_by_status():
        return dict(sql("SELECT status, count(*) FROM notes GROUP BY status"))

    db = tmp_path / "memory.db"
    assert not db.exists()
    client = mcp_spawn("--db", str(db), "--write", "--verbose")

    def call(tool, **args):
        reply, err = client.call_json(tool, **args)
        assert not err, tool
        assert_minimised(tool, reply)
        return reply

    text = f"The exhibits managed by {subject} stay indoors over winter."
    new = call("memory_remember", content=text, keywords="exhibits", tags=f"{first}, {span('tag')}, {second}",
               entities=[f"{subject}={alias}|{opening_tag}{markers['alias']}|unlisted{closing_tag}|{second}"],
               kind="decision")
    nid = new["id"]
    assert new["created"] is True
    assert sql("SELECT tags FROM notes WHERE id=?", nid) == [(f"{first} {second}",)]
    assert sql("SELECT aliases FROM entities WHERE name=?", subject) == [(f"{alias}|{second}",)]
    assert [r["id"] for r in call("memory_list")["results"]] == [nid]
    for tag in (first, second):
        assert [r["id"] for r in call("memory_search", query="exhibits", tags=tag)["results"]] == [nid]
    assert call("memory_search", query="exhibits", tags="unmatched")["count"] == 0
    related = call("memory_related", entity=alias)
    assert related["found"] and related["entity"] == subject and related["aliases"] == [alias, second]
    assert [note["id"] for note in related["notes"]] == [nid]

    # Seed ordinary history, so a duplicate cannot pass by adding a non-active row.
    with closing(store_mod.DecisionStore(str(db))) as store:
        store.add_older_version(nid, "The exhibit hall formerly opened early.", kind="decision", created_at=1)
        store.add_older_version(nid, "An old duplicate catalogue was retired.", kind="decision", created_at=2,
                                status="merged", rel="merged_into")
    before = notes_by_status()
    total_before, = sql("SELECT count(*) FROM notes")[0]
    assert before == {"active": 1, "merged": 1, "superseded": 1}
    repeated = call("memory_remember", content=text,
                    keywords=f"{edited}, {span('keyword')} {second} {edited} {opening_tag}{markers['empty']}",
                    kind="decision")
    assert repeated["id"] == nid and repeated["created"] is False
    assert notes_by_status() == before and sql("SELECT count(*) FROM notes") == [(total_before,)]
    assert sql("SELECT keywords FROM notes WHERE id=?", nid) == [(f"exhibits {edited} {second}",)]
    keyword_term = call("memory_search", query=edited, detail="more")
    assert [r["id"] for r in keyword_term["results"]] == [nid]
    field = store_mod.FIELD_KEYWORDS
    duplicate_postings = [r[0] for r in sql("SELECT term FROM postings WHERE note_id=? AND field=? ORDER BY term", nid, field)]
    assert duplicate_postings == sorted(["exhibit", edited, second])
    repeat_empty = call("memory_remember", content=text, keywords=span("empty"), kind="decision")
    assert repeat_empty["id"] == nid and repeat_empty["created"] is False
    after = notes_by_status()
    total_after, = sql("SELECT count(*) FROM notes")[0]
    assert before == after and total_before == total_after
    assert sql("SELECT keywords FROM notes WHERE id=?", nid) == [(f"exhibits {edited} {second}",)]

    only_name = subject + " Annex"
    only = call("memory_remember", content="A separate set of frames uses linen.", tags=span("empty"),
                entities=[f"{only_name}={span('empty')}"], kind="decision")
    assert only["created"]
    assert sql("SELECT tags FROM notes WHERE id=?", only["id"]) == [("",)]
    assert sql("SELECT aliases FROM entities WHERE name=?", only_name) == [("",)]

    with closing(store_mod.DecisionStore(str(db))) as store:
        assert store.set_fields(nid, tags=[edited, span("edit"), second])
        assert [n["id"] for n in store.search("exhibits", tags=edited)] == [nid]
        assert store.set_fields(only["id"], tags=span("edit"))
        eid = store.add_entity_aliases(subject, [span("edit"), f"{first} {span('edit')} catalogue"])
        assert store.detect_entities(alias) == {eid: 0.7}
        assert store.detect_entities(f"{first} catalogue") == {eid: 0.7}
        assert store.add_entity_aliases(only_name, [span("edit")])
    assert sql("SELECT tags FROM notes WHERE id=?", nid) == [(f"{edited} {second}",)]
    assert sql("SELECT tags FROM notes WHERE id=?", only["id"]) == [("",)]
    assert sql("SELECT aliases FROM entities WHERE name=?", subject) == [(f"{alias}|{second}|{first} catalogue",)]
    assert sql("SELECT aliases FROM entities WHERE name=?", only_name) == [("",)]

    edited_text = f"The exhibits managed by {subject} stay indoors throughout the spring."
    changed = call("memory_update", id=nid, content=edited_text, keywords=f"exhibits {span('edit')} {edited}")
    assert changed["id"] == nid and changed["updated"]
    assert sql("SELECT keywords FROM notes WHERE id=?", nid) == [(f"exhibits [REDACTED] {edited}",)]
    assert call("memory_get", id=nid)["content"] == edited_text
    assert client.close() == 0

    stderr = client.stderr()
    assert "tools/call memory_remember" in stderr and "tools/call memory_update" in stderr
    assert_minimised("MCP stderr", stderr)
    assert text not in stderr and edited_text not in stderr
    for reply in client.raw_lines:
        assert_minimised("wire reply", reply)
    usage = sql("SELECT kind, query FROM usage_log ORDER BY id")
    assert ("search", edited) in usage
    assert_minimised("usage log", usage)
    for table in ("notes", "entities", "note_entities", "postings", "note_links"):
        assert_minimised(table, sql(f"SELECT * FROM {table}"))

    dest = tmp_path / "fresh.json"
    assert not dest.exists()
    cli = Path(__file__).resolve().parents[1] / "hermind_cli.py"
    export = subprocess.run([sys.executable, str(cli), "export", "--db", str(db), "--out", str(dest), "--json"],
                            text=True, capture_output=True, check=True)
    assert_minimised("export stdout/stderr", export.stdout + export.stderr)
    raw = dest.read_text(encoding="utf-8")
    assert_minimised("fresh export", raw)
    rows = {r["id"]: r for r in json.loads(raw)}
    assert len(rows) == sum(notes_by_status().values())
    assert rows[nid]["tags"] == f"{edited} {second}" and edited in rows[nid]["keywords"].split()
    assert {"name": subject, "aliases": [alias, second, f"{first} catalogue"]} in rows[nid]["entities"]
    assert rows[only["id"]]["tags"] == "" and {"name": only_name, "aliases": []} in rows[only["id"]]["entities"]

    if evidence := os.getenv("FIELD_EVIDENCE_DIR"):
        result = {"duplicate": {"id": nid, "created": repeated["created"], "statuses_before": before,
                                "statuses_after": after, "total_before": total_before, "total_after": total_after,
                                "keyword_postings": duplicate_postings, "all_private_created": repeat_empty["created"]},
                  "assertions_passed": ["tag list and filter", "entity alias lookup", "public duplicate keyword search",
                                        "literal public [REDACTED] retained", "empty private-only tag and alias",
                                        "tag/alias/keyword edit", "SQLite and postings",
                                        "new all-status JSON export", "MCP wire replies", "MCP stderr", "SQLite usage_log"],
                  "export_exit": export.returncode, "export_rows": len(rows), "mcp_exit": client.proc.returncode,
                  "mcp_replies_checked": len(client.raw_lines), "mcp_stderr_lines_checked": len(stderr.splitlines()),
                  "usage_log_rows_checked": len(usage), "private_markers_found": False}
        (Path(evidence) / f"{first}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
