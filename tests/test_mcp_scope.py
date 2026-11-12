"""Scope: what the Hermes plugin writes under its `scope` is what the MCP server reads (same rule as the store)."""

import json

import pytest


def _texts(c, query="Falcon", **kw):
    got, err = c.call_json("memory_search", query=query, detail="more", **kw)
    assert not err
    return {n["content"] for n in got["results"]}


@pytest.fixture
def three_scopes(store_mod, tmp_path):
    db = str(tmp_path / "scopes.db")
    for scope, text in (("default", "Falcon default-scope decision about hosting"),
                        ("alice", "Falcon alice-only decision about hosting"),
                        ("bob", "Falcon bob-only decision about hosting"),
                        ("shared", "Falcon shared household decision about hosting")):
        s = store_mod.DecisionStore(db, scope=scope)
        s.add(text, kind="decision", keywords="hosting falcon", entities=["Falcon"])
        s.close()
    return db


def test_plugin_notes_are_visible_to_mcp_with_default_scope(provider, mcp_spawn, tmp_path):
    """A note written by the real plugin provider (default config) is found by the MCP server on the same file."""
    out = json.loads(provider.handle_tool_call("memory_remember", {
        "content": "Plugin wrote this: the staging database lives on host Orca.", "kind": "tech",
        "keywords": "staging database host orca"}))
    assert out["created"]
    db = provider._store.db_path
    c = mcp_spawn("--db", db)
    assert any("host Orca" in t for t in _texts(c, "staging database"))
    # and the other direction: what MCP writes the plugin reads
    w = mcp_spawn("--db", db, "--write", name="w")
    w.call("memory_remember", content="MCP wrote this: the cache is Redis on host Manta.", keywords="cache redis manta")
    assert "Manta" in provider.handle_tool_call("memory_search", {"query": "cache redis", "detail": "more"})


def test_default_scope_sees_default_and_shared_only(mcp_spawn, three_scopes):
    c = mcp_spawn("--db", three_scopes)
    assert _texts(c) == {"Falcon default-scope decision about hosting", "Falcon shared household decision about hosting"}


def test_scope_option_and_env(mcp_spawn, three_scopes):
    a = mcp_spawn("--db", three_scopes, "--scope", "alice", name="a")
    assert _texts(a) == {"Falcon alice-only decision about hosting", "Falcon shared household decision about hosting"}
    b = mcp_spawn("--db", three_scopes, env={"HERMIND_SCOPE": "bob"}, name="b")
    assert "Falcon bob-only decision about hosting" in _texts(b) and not any("alice" in t for t in _texts(b))


def test_star_scope_reads_everything(mcp_spawn, three_scopes):
    c = mcp_spawn("--db", three_scopes, "--scope", "*")
    assert len(_texts(c)) == 4
    assert c.call_json("memory_list")[0]["count"] == 4


def test_list_and_related_follow_scope(mcp_spawn, three_scopes):
    c = mcp_spawn("--db", three_scopes, "--scope", "alice")
    assert c.call_json("memory_list")[0]["count"] == 2
    rel, _ = c.call_json("memory_related", entity="Falcon")
    assert {n["content"] for n in rel["notes"]} == {"Falcon alice-only decision about hosting",
                                                    "Falcon shared household decision about hosting"}


def test_get_and_writes_by_id_cannot_cross_scopes(mcp_spawn, three_scopes):
    import sqlite3
    con = sqlite3.connect(three_scopes)
    bob_id = con.execute("SELECT id FROM notes WHERE scope='bob'").fetchone()[0]
    con.close()
    c = mcp_spawn("--db", three_scopes, "--scope", "alice", "--write", "--tools", "get,update,forget")
    for tool, args in (("memory_get", {"id": bob_id}), ("memory_update", {"id": bob_id, "content": "hijacked"}),
                       ("memory_forget", {"id": bob_id})):
        res, err = c.call_json(tool, **args)
        assert err is True and "no note" in res["error"], tool
    con = sqlite3.connect(three_scopes)
    assert con.execute("SELECT content FROM notes WHERE id=?", (bob_id,)).fetchone()[0].startswith("Falcon bob-only")


def test_writes_go_to_the_configured_scope(mcp_spawn, three_scopes):
    import sqlite3
    c = mcp_spawn("--db", three_scopes, "--scope", "alice", "--write")
    c.call("memory_remember", content="Alice's new decision about the lab network", keywords="lab network")
    star = mcp_spawn("--db", three_scopes, "--scope", "*", "--write", name="s")
    star.call("memory_remember", content="Star mode decision about the garden shed", keywords="garden shed")
    rows = dict(sqlite3.connect(three_scopes).execute(
        "SELECT substr(content,1,10), scope FROM notes WHERE content LIKE 'Alice''s new%' OR content LIKE 'Star mode%'"))
    assert rows == {"Alice's ne": "alice", "Star mode ": "default"}
