"""The `claude-code` reader of `hermind import`: a configuration folder, one memory folder, one MEMORY.md."""

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import import_core  # noqa: E402
import reader_claude_code as rcc  # noqa: E402
from store import DecisionStore  # noqa: E402
from store_common import KINDS, parse_valid  # noqa: E402

MODIFIED = "2025-03-04T10:20:30.000Z"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for k in ("HERMIND_DB_KEY", "HERMIND_DB_KEY_FILE", "HERMIND_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def enc(p) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def notes(db):
    s = DecisionStore(str(db))
    try:
        return [dict(r) for r in s.conn.execute("SELECT * FROM notes WHERE status='active' ORDER BY id")]
    finally:
        s.close()


INDEX = ("- [Codename](codename.md) — the staging codename\n- [Deploy day](deploy.md) - which weekday\n"
         "- [Cache](cache.md): about the cache\n- [Gone](gone.md) — this file is missing\n- A plain bullet about Redis on 6380\n")


def memory_folder(mem: Path, tag: str) -> None:
    w(mem / "MEMORY.md", INDEX.replace("Codename", f"Codename {tag}"))
    w(mem / "codename.md", f"---\nname: Staging codename\ndescription: Codename of staging {tag}\nmetadata:\n  node_type: memory\n"
                           f"  type: project\n  originSessionId: 1b2c\n  modified: {MODIFIED}\n---\nThe staging codename is blue-heron-{tag}.\n")
    w(mem / "deploy.md", f"---\nname: deploy-day\ndescription: Deploy weekday\ntype: reference\n---\nDeploys go out on Thursdays ({tag}).\n")
    w(mem / "cache.md", f"Entries stay for ten minutes ({tag}).\n")
    w(mem / "mood.md", f"---\nname: mood\ntype: feedback\nflavour: odd\n---\nThe user likes terse answers ({tag}).\n")


@pytest.fixture
def config(tmp_path):
    """A configuration folder S with: project atlas (folder exists), project ghost (folder does not exist), project beta
    (only an AGENTS.md), user instructions and rules, user-scope subagent memory, transcripts and a credentials file."""
    work = tmp_path / "work"
    atlas, beta = work / "atlas", work / "beta"
    S = tmp_path / "S"
    # the project folders
    w(atlas / "CLAUDE.md", "# Atlas\nBuild with make.\n\n## Testing\nRun pytest with Redis up.\n\nSee @docs/setup.md\n")
    w(atlas / "CLAUDE.local.md", "My local sandbox is on port 9000.\n")
    w(atlas / ".claude" / "rules" / "api.md", "---\npaths:\n  - src/api/**/*.ts\n---\nReturn 404 for unknown ids.\n")
    w(atlas / ".claude" / "agent-memory" / "planner" / "MEMORY.md", "- [Plan style](style.md) — short plans\n")
    w(atlas / ".claude" / "agent-memory" / "planner" / "style.md", "---\nname: plan-style\ntype: feedback\n---\nPlans fit on one page.\n")
    w(atlas / "AGENTS.md", "# Agents\nThis AGENTS.md must not be read.\n")
    w(beta / "AGENTS.md", "# Beta\nBeta uses tabs everywhere.\n")
    # the configuration folder
    memory_folder(S / "projects" / enc(atlas) / "memory", "alpha")
    memory_folder(S / "projects" / "-nowhere-ghost-app" / "memory", "ghost")
    w(S / "projects" / enc(beta) / "memory" / "b.md", "Beta fact: the beta budget is small.\n")
    for P in (enc(atlas), "-nowhere-ghost-app"):
        w(S / "projects" / P / "0a1b-session.jsonl", '{"secret": "never read"}\n')
    w(S / "CLAUDE.md", "# Me\nPrefer short answers.\n\n## Spelling\nUse British English. See @~/style.md\n")
    w(S / "rules" / "style.md", "Always write tests.\n")
    w(S / "agent-memory" / "reviewer" / "MEMORY.md", "Remember to check migrations before approving.\n")
    w(S / "settings.json", "{}\n")
    w(S / "agents" / "x.md", "agent definition\n")
    w(S / "skills" / "y" / "SKILL.md", "skill definition\n")
    w(S / ".credentials.json", '{"token": "not for you"}\n')
    return S


def run(src, db, **kw):
    return import_core.run_import(str(src), db, **kw)


# -- check 1: index lines -------------------------------------------------------------------------------------------------

def test_no_note_repeats_an_index_line_the_missing_file_line_and_the_bullet_are_notes(tmp_path, config):
    out = run(config, tmp_path / "c.db", apply=True, backup=False)
    assert out["reader"] == "claude-code" and out["reader_detected"]
    for label in ("atlas", "nowhere-ghost-app"):
        mine = [e for e in out["entries"] if e["origin"].startswith(f"memory/{label}/MEMORY.md")]
        assert [e["origin"] for e in mine] == [f"memory/{label}/MEMORY.md#1", f"memory/{label}/MEMORY.md#2"]
    texts = [n["content"] for n in notes(tmp_path / "c.db")]
    assert any("Gone — this file is missing" in t for t in texts) and not any("[Gone]" in t or "(gone.md)" in t for t in texts)
    assert any("A plain bullet about Redis on 6380" in t for t in texts)
    assert not any("](" in t for t in texts)                                # no link markup survives anywhere
    for title in ("Deploy day", "Cache", "Codename"):                          # the lines that link to a file in the folder make no note
        assert not any(t.endswith(("which weekday", "about the cache", "the staging codename")) for t in texts), title


def test_index_title_and_summary_join_the_topic_note_as_keywords_and_title_is_a_subject_when_needed(tmp_path, config):
    run(config, tmp_path / "c.db", apply=True, backup=False)
    by = {n["content"]: n for n in notes(tmp_path / "c.db")}
    code = next(n for t, n in by.items() if "blue-heron-alpha" in t)
    assert "Codename alpha" in code["keywords"] and "the staging codename" in code["keywords"]
    cache = next(n for t, n in by.items() if "Entries stay for ten minutes (alpha)" in t)       # no name, no title: the index title is the subject
    assert cache["content"].splitlines()[0] == "atlas › Cache" and "about the cache" in cache["keywords"]


# -- check 2: topic files ---------------------------------------------------------------------------------------------------

def test_topic_files_kinds_from_type_dates_from_modified_and_the_label_in_every_project_note(tmp_path, config):
    out = run(config, tmp_path / "c.db", apply=True, backup=False)
    rows = notes(tmp_path / "c.db")
    code = next(n for n in rows if "blue-heron-alpha" in n["content"])
    assert code["kind"] == "tech" and abs(code["created_at"] - parse_valid(MODIFIED)) < 1                     # nested front matter
    deploy = next(n for n in rows if "Deploys go out on Thursdays (alpha)" in n["content"])
    assert deploy["kind"] == "tech"                                                                              # flat, type: reference
    mood = next(n for n in rows if "terse answers (alpha)" in n["content"])
    assert mood["kind"] == "preference"
    plain = next(n for n in rows if "Entries stay for ten minutes (alpha)" in n["content"])
    assert plain["kind"] in KINDS                                                                    # none: from the text
    for label, tag in (("atlas", "alpha"), ("nowhere-ghost-app", "ghost")):
        mine = [e for e in out["entries"] if e["origin"].startswith(f"memory/{label}/") and e["outcome"] == "new"]
        assert len(mine) == 6
        for n in rows:
            if f"-{tag}" in n["content"] or f"({tag})" in n["content"] or f"holds {tag}" in n["content"]:
                assert n["content"].split("\n\n")[0].lower().count(label) >= 1 or label in n["content"].split("\n")[0]
    assert sum(1 for n in rows if n["content"].startswith("atlas")) >= 5


def test_nested_front_matter_counts_as_top_level_and_a_top_level_key_wins(tmp_path):
    mem = tmp_path / "mem"
    w(mem / "a.md", f"---\ntype: feedback\nmetadata:\n  type: project\n  modified: {MODIFIED}\n---\nUser wants tabs not spaces.\n")
    w(mem / "b.md", "---\nmetadata:\n  type: reference\n---\nThe wiki lives at wiki.example.test.\n")
    w(mem / "c.md", "---\ntype: weather\n---\nNobody knows what weather means here.\n")
    w(mem / "MEMORY.md", "- [A](a.md) — a\n")
    out = run(mem, tmp_path / "m.db", apply=True, backup=False)
    assert out["reader"] == "claude-code" and out["counts"]["new"] == 3
    kinds = {n["content"].split("\n")[-1]: n["kind"] for n in notes(tmp_path / "m.db")}
    assert kinds["User wants tabs not spaces."] == "preference"                       # top-level type: feedback beats nested project
    assert kinds["The wiki lives at wiki.example.test."] == "tech"
    assert not any("type" in r.split("(")[0] for r in out["remarks"] if "front matter keys" in r)   # an unknown value: no remark


def test_the_unused_keys_remark_never_names_the_known_keys(tmp_path, config):
    out = run(config, tmp_path / "c.db")
    (r,) = [r for r in out["remarks"] if r.startswith("front matter keys")]
    assert "flavour (2 files)" in r
    for known in ("metadata", "node_type", "originSessionId", "modified", "type"):
        assert known not in r


# -- check 3 and 4: instructions --------------------------------------------------------------------------------------------

def test_user_instructions_and_rules(tmp_path, config):
    out = run(config, tmp_path / "c.db")
    user = [e for e in out["entries"] if e["group"] == "user instructions"]
    assert [e["origin"] for e in user] == ["user-instructions/CLAUDE.md#1", "user-instructions/CLAUDE.md#2",
                                           "user-instructions/rules/style.md#1"]
    assert user[0]["text"].startswith("Me Prefer short answers") and user[1]["text"].startswith("Me › Spelling Use British")
    assert any("@~/style.md" in r for r in out["remarks"] if "@ import" in r)


def test_the_found_projects_files_agents_md_rule_and_the_third_project(tmp_path, config):
    out = run(config, tmp_path / "c.db", apply=True, backup=False)
    origins = [e["origin"] for e in out["entries"]]
    assert "project-instructions/atlas/CLAUDE.md#1" in origins and "project-instructions/atlas/CLAUDE.md#2" in origins
    assert "project-instructions/atlas/CLAUDE.local.md#1" in origins
    assert "project-instructions/atlas/.claude/rules/api.md#1" in origins
    assert not any("atlas/AGENTS.md" in o for o in origins)                         # a CLAUDE.md exists: AGENTS.md is not read
    assert "project-instructions/beta/AGENTS.md#1" in origins                       # only an AGENTS.md: read
    assert "project-agent-memory/atlas/planner/style.md" in origins and "project-agent-memory/atlas/planner/MEMORY.md#1" not in origins
    assert "agent-memory/reviewer/MEMORY.md#1" in origins                           # user-scope subagent memory
    texts = [n["content"] for n in notes(tmp_path / "c.db")]
    assert not any("must not be read" in t for t in texts)
    rule = next(t for t in texts if "Return 404" in t)
    assert "applies to: src/api/**/*.ts" in rule and rule.startswith("atlas")
    assert next(t for t in texts if "Plans fit on one page" in t).startswith("atlas/planner")
    assert next(t for t in texts if "Remember to check migrations" in t).startswith("reviewer")


# -- check 5: files that must stay closed -------------------------------------------------------------------------------------

def test_transcripts_and_credentials_are_never_opened(tmp_path, config):
    closed = [*config.glob("projects/*/*.jsonl"), config / ".credentials.json"]
    for p in closed:
        p.chmod(0)
    try:
        out = run(config, tmp_path / "c.db", apply=True, backup=False)
    finally:
        for p in closed:
            p.chmod(0o644)
    assert out["ok"] and out["counts"]["unreadable"] == 0
    joined = "\n".join(out["remarks"])
    assert "2 session transcript(s)" in joined and "0a1b-session.jsonl" in joined
    assert "1 project(s) have no folder" in joined and "nowhere-ghost-app" in joined
    assert "1 project(s) have an AGENTS.md that was not read because a CLAUDE.md exists" in joined and "atlas" in joined
    assert "@ import(s) were not followed" in joined and "@docs/setup.md" in joined
    assert ".credentials.json" in joined and "agents/" in joined and "skills/" in joined
    assert "CLAUDE.md files in a project's subfolders are not searched" in joined
    assert not any("never read" in n["content"] or "not for you" in n["content"] for n in notes(tmp_path / "c.db"))


def test_the_subfolders_note_is_absent_when_no_project_folder_was_found(tmp_path):
    S = tmp_path / "S"
    w(S / "projects" / "-nowhere-x" / "memory" / "a.md", "A fact.\n")
    out = run(S, tmp_path / "c.db", reader="claude-code")
    assert not any("subfolders" in r for r in out["remarks"]) and any("no folder on this computer" in r for r in out["remarks"])


# -- check 6: exact group counts ---------------------------------------------------------------------------------------------

def test_group_counts_are_exact_excluding_project_instructions_keeps_every_memory_note_and_apply_twice(tmp_path, config):
    db = tmp_path / "c.db"
    base = run(config, db)
    names = [g["name"] for g in base["groups"]]
    assert names == ["memory · nowhere-ghost-app", "memory · atlas", "memory · beta", "agent memory · reviewer", "user instructions",
                     "instructions · atlas", "instructions · beta", "agent memory · atlas/planner"] or \
        set(names) >= {"memory · atlas", "memory · beta", "user instructions", "instructions · atlas", "instructions · beta",
                       "agent memory · reviewer", "agent memory · atlas/planner", "memory · nowhere-ghost-app"}
    assert "other" not in names
    for k in import_core.OUTCOMES:
        assert sum(g["counts"][k] for g in base["groups"]) == base["counts"][k]
    assert sum(g["total"] for g in base["groups"]) == base["total"]
    out = run(config, db, exclude=["project-instructions/*"])
    for e0, e1 in zip(base["entries"], out["entries"]):
        assert (e1["outcome"] == "excluded") == e0["origin"].startswith("project-instructions/")
        if not e0["origin"].startswith("project-instructions/"):
            assert e1["outcome"] == e0["outcome"]
    assert all(g["counts"]["new"] > 0 for g in out["groups"] if g["name"].startswith("memory"))
    first = run(config, db, apply=True, backup=False)
    second = run(config, db, apply=True, backup=False)
    assert second["counts"]["new"] == 0 and second["counts"]["duplicate"] == first["counts"]["new"] and second["batch"] is None
    for g in base["groups"]:                       # every group excluded by its own pattern changes only that group
        out = run(config, tmp_path / "other.db", exclude=[g["exclude"]])
        gone = {e["origin"] for e in out["entries"] if e["outcome"] == "excluded"}
        assert gone == {e["origin"] for e in base["entries"] if e["group"] == g["name"]}


def test_a_group_pattern_for_a_label_with_brackets_matches_only_that_label(tmp_path):
    S = tmp_path / "S"
    w(S / "projects" / "-nowhere-a[1]b" / "memory" / "x.md", "Fact in bracket project.\n")
    w(S / "projects" / "-nowhere-a1b" / "memory" / "x.md", "Fact in the plain project.\n")
    out = run(S, tmp_path / "c.db", reader="claude-code")
    pats = {g["name"]: g["exclude"] for g in out["groups"]}
    assert pats["memory · nowhere-a[1]b"] == "memory/nowhere-a[[]1[]]b/*"
    out2 = run(S, tmp_path / "c.db", reader="claude-code", exclude=[pats["memory · nowhere-a[1]b"]])
    assert [e["outcome"] for e in out2["entries"]] == ["excluded", "new"] or sorted(e["outcome"] for e in out2["entries"]) == ["excluded", "new"]


# -- check 7: detection ------------------------------------------------------------------------------------------------------

def test_detection_of_the_folder_one_memory_folder_and_one_memory_md(tmp_path, config):
    assert import_core.detect_reader(config) == ("claude-code", [])
    mem = config / "projects" / "-nowhere-ghost-app" / "memory"
    assert import_core.detect_reader(mem) == ("claude-code", [])
    assert import_core.detect_reader(mem / "MEMORY.md") == ("claude-code", [])
    only_topics = tmp_path / "topics"
    w(only_topics / "a.md", "---\nname: a\ntype: feedback\n---\nA fact.\n")
    assert import_core.detect_reader(only_topics) == ("claude-code", [])
    nodetype = tmp_path / "nodetype"
    w(nodetype / "a.md", "---\nmetadata:\n  node_type: memory\n---\nA fact.\n")
    assert import_core.detect_reader(nodetype) == ("claude-code", [])
    conf2 = tmp_path / "conf2"                                                       # CLAUDE.md together with settings.json
    w(conf2 / "CLAUDE.md", "Be brief.\n")
    w(conf2 / "settings.json", "{}")
    assert import_core.detect_reader(conf2) == ("claude-code", [])


def test_a_hermes_memory_md_and_a_plain_notes_folder_are_not_claude_code(tmp_path):
    hermes = tmp_path / "hermes"
    w(hermes / "MEMORY.md", "The VPS has 4GB RAM.\n§\n- [x](y.md) looks like an index line\n")
    w(hermes / "USER.md", "User prefers short replies.\n")
    assert rcc.detect_claude_code(hermes) == 0 and rcc.detect_claude_code(hermes / "MEMORY.md") == 0
    assert import_core.detect_reader(hermes)[0] == "hermes-builtin"
    plain = tmp_path / "notes"
    w(plain / "a.md", "---\ntitle: x\ntype: essay\n---\nSome note.\n")
    w(plain / "projects" / "readme.md", "A folder named projects.\n")
    assert rcc.detect_claude_code(plain) == 0 and import_core.detect_reader(plain) == ("markdown", [])
    notes_only = tmp_path / "plain2"
    w(notes_only / "MEMORY.md", "Just a file with no links.\n")
    assert rcc.detect_claude_code(notes_only) == 0


def test_the_sample_memory_folder_is_detected_and_imported_with_kinds_and_dates(tmp_path):
    mem = tmp_path / "samples" / "claude-code-memory"
    w(mem / "MEMORY.md", "- [Codename](codename.md) — staging codename\n- [Deploy](deploy.md) — weekday\n")
    w(mem / "codename.md", "---\nname: Atlas staging codename\ndescription: Codename of the Atlas staging environment\ntype: project\n---\n"
                           "The staging codename for project Atlas is blue-heron-42.\n")
    w(mem / "deploy.md", f"---\nname: atlas-deploy\ndescription: Deploy day\nmetadata:\n  node_type: memory\n  type: feedback\n"
                         f"  originSessionId: 42c0\n  modified: {MODIFIED}\n---\nAtlas deploys go out on Thursdays.\n")
    out = run(mem, tmp_path / "s.db", apply=True, backup=False)
    assert out["reader"] == "claude-code" and out["counts"]["new"] == 2 and len(out["groups"]) == 1
    rows = {n["content"].split("\n")[-1]: n for n in notes(tmp_path / "s.db")}
    assert rows["Atlas deploys go out on Thursdays."]["kind"] == "preference"
    assert abs(rows["Atlas deploys go out on Thursdays."]["created_at"] - parse_valid(MODIFIED)) < 1
    assert not any(o.startswith("MEMORY.md") for o in (e["origin"] for e in out["entries"]))
    assert not any("unused" in r or "node_type" in r for r in out["remarks"])


def test_a_memory_folder_under_projects_gets_its_label_from_the_project_folder(tmp_path):
    proj = tmp_path / "work" / "My Project"
    proj.mkdir(parents=True)
    mem = tmp_path / "S" / "projects" / enc(proj) / "memory"
    w(mem / "a.md", "---\ntype: project\n---\nThe release is on a Friday.\n")
    out = run(mem, tmp_path / "m.db", apply=True, backup=False)
    assert out["groups"][0]["name"] == "memory · My Project"
    assert notes(tmp_path / "m.db")[0]["content"].startswith("My Project")
    single = run(mem / "a.md", tmp_path / "m2.db", reader="markdown")
    assert single["reader"] == "markdown"


def test_a_lone_memory_md_makes_a_note_of_every_index_line(tmp_path):
    f = w(tmp_path / "MEMORY.md", "- [Codename](codename.md) — staging codename\n- plain bullet here\n")
    w(tmp_path / "codename.md", "---\ntype: project\n---\nThe codename is blue-heron.\n")
    out = run(f, tmp_path / "m.db")
    assert out["reader"] == "claude-code" and out["counts"]["new"] == 2
    assert any("topic file(s) next to MEMORY.md were not read" in r for r in out["remarks"])


# -- check 8: decoding project folder names --------------------------------------------------------------------------------

def P(*parts) -> str:
    return "".join("-" + enc(p) for p in parts)


def test_folder_names_with_dots_underscores_dashes_and_spaces_decode(tmp_path):
    names = [".config", "my_app", "my-app2", "has space", "dot.dot", "a--b", "日本"]
    for n in names:
        (tmp_path / "home" / "maya" / n).mkdir(parents=True, exist_ok=True)
    for n in names:
        # (my_app and my-app2 differ: only one folder encodes to each name)
        found, count = rcc.find_project_folder(P("home", "maya", n), tmp_path)
        assert (found, count) == (tmp_path / "home" / "maya" / n, 1), n


def test_two_candidate_folders_or_none_mean_no_project_folder(tmp_path):
    for n in ("a.b", "a_b", "a b"):
        (tmp_path / "work" / n).mkdir(parents=True)
    assert rcc.find_project_folder(P("work", "a.b"), tmp_path) == (None, 2)
    assert rcc.find_project_folder(P("work", "zzz"), tmp_path) == (None, 0)
    (tmp_path / "work" / "files").mkdir()
    (tmp_path / "work" / "files" / "x").write_text("a file, not a folder")
    assert rcc.find_project_folder(P("work", "files", "x"), tmp_path) == (None, 0)
    assert rcc.find_project_folder("not-encoded", tmp_path) == (None, 0)


def test_a_folder_that_cannot_be_listed_is_skipped_and_a_drive_letter_name_decodes_below_a_given_root(tmp_path):
    (tmp_path / "work" / "locked").mkdir(parents=True)
    (tmp_path / "work" / "open").mkdir()
    (tmp_path / "work" / "locked").chmod(0)
    try:
        assert rcc.find_project_folder(P("work", "locked", "inner"), tmp_path) == (None, 0)
        assert rcc.find_project_folder(P("work", "open"), tmp_path)[1] == 1
    finally:
        (tmp_path / "work" / "locked").chmod(0o755)
    (tmp_path / "Users" / "maya").mkdir(parents=True)
    assert rcc.find_project_folder("C--Users-maya", tmp_path) == (tmp_path / "Users" / "maya", 1)


def test_two_projects_with_the_same_folder_name_get_the_encoded_name_as_label(tmp_path):
    S = tmp_path / "S"
    a, b = tmp_path / "one" / "app", tmp_path / "two" / "app"
    for p in (a, b):
        p.mkdir(parents=True)
    w(S / "projects" / enc(a) / "memory" / "x.md", "Fact from the first app.\n")
    w(S / "projects" / enc(b) / "memory" / "x.md", "Fact from the second app.\n")
    out = run(S, tmp_path / "c.db", reader="claude-code")
    labels = [g["name"] for g in out["groups"]]
    assert all(n.startswith("memory · ") and n != "memory · app" for n in labels) and len(set(labels)) == 2
    assert all(enc(p)[1:] in "".join(labels) for p in (a, b))


def test_auto_memory_directory_is_read_as_a_custom_folder(tmp_path, monkeypatch):
    custom = tmp_path / "home" / "elsewhere"
    w(custom / "a.md", "---\ntype: user\n---\nThe user lives in Porto.\n")
    S = tmp_path / "S"
    w(S / "CLAUDE.md", "Be brief.\n")
    w(S / "settings.json", '{"autoMemoryDirectory": "~/elsewhere"}')
    out = run(S, tmp_path / "c.db", apply=True, backup=False)
    assert [e["origin"] for e in out["entries"] if e["group"] == "memory · custom folder"] == ["custom-memory/a.md"]
    S2 = tmp_path / "S2"
    w(S2 / "CLAUDE.md", "Be brief.\n")
    w(S2 / "settings.json", '{"autoMemoryDirectory": "relative/dir"}')
    out2 = run(S2, tmp_path / "c2.db")
    assert any("not an absolute path" in r for r in out2["remarks"])


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_links_inside_a_memory_folder_are_reported_and_never_read(tmp_path):
    outside = tmp_path / "outside"
    w(outside / "host.md", "Zorblax backup runs at 03:00 on node seven.\n")
    w(outside / "dir" / "deep.md", "Quillon reports are due on Fridays.\n")
    mem = tmp_path / "mem"
    memory_folder(mem, "ok")
    os.symlink(outside / "host.md", mem / "escape.md")
    os.symlink(outside / "dir", mem / "linked")
    db = tmp_path / "t.db"
    res = import_core.run_import(str(mem), db, apply=True, backup=False)
    bad = {e["origin"]: e["reason"] for e in res["entries"] if e["outcome"] == "unreadable"}
    assert sorted(bad) == ["escape.md", "linked/"] and all("symbolic link" in r for r in bad.values())
    assert not any("Zorblax" in n["content"] or "Quillon" in n["content"] for n in notes(db))
    assert res["created"] >= 4


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_linked_instruction_files_of_a_config_folder_are_reported_and_never_read(tmp_path):
    outside = tmp_path / "outside"
    w(outside / "hostname.md", "# Secrets\nZorblax backup runs at 03:00 on node seven.\n")
    w(outside / "rules" / "r.md", "# Rule\nQuillon reports are due on Fridays.\n")
    cfg = tmp_path / "cfg"
    w(cfg / "settings.json", "{}")
    w(cfg / "CLAUDE.md", "# Prefs\nAnswer briefly.\n")
    os.symlink(outside / "rules", cfg / "rules")
    (cfg / "projects").mkdir()
    proj = tmp_path / "work" / "atlas"
    w(proj / "AGENTS.md", "# Atlas\nAtlas deploys on Thursdays.\n")
    os.symlink(outside / "hostname.md", proj / "CLAUDE.md")
    w(cfg / "projects" / enc(proj) / "memory" / "MEMORY.md", "- A plain bullet about Redis on 6380\n")
    os.symlink(outside / "hostname.md", cfg / "projects" / enc(proj) / "memory" / "link.md")
    res = import_core.run_import(str(cfg), tmp_path / "t.db", apply=True, backup=False,
                                 )
    bad = {e["origin"] for e in res["entries"] if e["outcome"] == "unreadable"}
    assert "user-instructions/rules/" in bad and any(o.endswith("link.md") for o in bad)
    assert not any("Zorblax" in n["content"] or "Quillon" in n["content"] for n in notes(tmp_path / "t.db"))


def _linked_world(tmp_path):
    """A secret-bearing 'outside' tree that links must never lead to, and a config folder + project to put links in."""
    outside = tmp_path / "outside"
    w(outside / "CLAUDE.md", "# Secrets\nZorblax backup runs at 03:00 on node seven.\n")
    w(outside / "rules" / "r.md", "# Rule\nQuillon reports are due on Fridays.\n")
    memory_folder(outside / "agent-memory" / "helper", "outside")
    w(outside / "projects" / "-x" / "memory" / "MEMORY.md", "- Plinth rota is on Mondays\n")
    cfg = tmp_path / "cfg"
    w(cfg / "settings.json", "{}")
    w(cfg / "CLAUDE.md", "# Prefs\nAnswer briefly.\n")
    proj = tmp_path / "work" / "atlas"
    w(proj / "AGENTS.md", "# Atlas\nAtlas deploys on Thursdays.\n")
    (cfg / "projects").mkdir()
    w(cfg / "projects" / enc(proj) / "memory" / "MEMORY.md", "- A plain bullet about Redis on 6380\n")
    return outside, cfg, proj


def _import_unreadable(cfg, tmp_path):
    res = import_core.run_import(str(cfg), tmp_path / "t.db", apply=True, backup=False)
    bad = {e["origin"]: e["reason"] for e in res["entries"] if e["outcome"] == "unreadable"}
    text = " ".join(n["content"] for n in notes(tmp_path / "t.db"))
    return bad, text


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_projects_folder_that_is_a_link_is_reported_not_followed(tmp_path):
    outside, cfg, proj = _linked_world(tmp_path)
    import shutil
    shutil.rmtree(cfg / "projects")
    os.symlink(outside / "projects", cfg / "projects")
    bad, text = _import_unreadable(cfg, tmp_path)
    assert "projects/" in bad and "symbolic link" in bad["projects/"]
    assert "Plinth" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_project_dot_claude_folder_that_is_a_link_is_reported_and_nothing_below_it_is_read(tmp_path):
    outside, cfg, proj = _linked_world(tmp_path)
    w(outside / "CLAUDE.md", "# Secrets\nZorblax backup runs at 03:00 on node seven.\n")
    os.symlink(outside, proj / ".claude")                        # outside/ holds CLAUDE.md, rules/ and agent-memory/
    bad, text = _import_unreadable(cfg, tmp_path)
    assert any(o.endswith(".claude/") and "symbolic link" in r for o, r in bad.items()), bad
    assert sum(o.endswith(".claude/") for o in bad) == 1
    assert "Zorblax" not in text and "Quillon" not in text and "blue-heron-outside" not in text
    assert "Redis" in text                                        # the real memory folder is still read


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_project_agent_memory_folder_that_is_a_link_is_reported_and_not_entered(tmp_path):
    outside, cfg, proj = _linked_world(tmp_path)
    (proj / ".claude").mkdir()
    os.symlink(outside / "agent-memory", proj / ".claude" / "agent-memory")
    bad, text = _import_unreadable(cfg, tmp_path)
    assert any(o.startswith("project-agent-memory/") and "symbolic link" in r for o, r in bad.items()), bad
    assert "blue-heron-outside" not in text and "Redis" in text


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_memory_md_named_as_the_source_is_read_even_when_it_is_a_link(tmp_path):
    """The user named this very file: following it is the documented rule for every single-file source."""
    real = tmp_path / "dotfiles" / "claude-memory.md"
    w(real, "- A plain bullet about Redis on 6380\n- Another bullet about the Zorblax backup at 03:00\n")
    mem = tmp_path / "mem"
    mem.mkdir()
    os.symlink(real, mem / "MEMORY.md")
    res = import_core.run_import(str(mem / "MEMORY.md"), tmp_path / "t.db", apply=True, backup=False)
    assert not [e for e in res["entries"] if e["outcome"] == "unreadable"]
    assert "Redis" in " ".join(n["content"] for n in notes(tmp_path / "t.db")) and res["created"] >= 1
