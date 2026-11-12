"""The docs say what the code does: the import-export page names every outcome, option, reader and record key, the
module table covers every runtime module, docs/ holds exactly the five English pages, and every relative link works."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermind_cli  # noqa: E402
import import_core  # noqa: E402

DOCS = {"how-it-works.md", "import-export.md", "limitations.md", "privacy.md", "setup.md"}
CHANGED = ["README.md", "README.zh-TW.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
           *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").glob("*.md"))]
PAGE = "docs/import-export.md"
OTHER_PRODUCTS = ("mem0", "zep", "letta", "memgpt", "hindsight", "graphiti", "cognee", "supermemory", "langmem")
RUNTIME_MODULES = sorted(p.name for p in ROOT.glob("*.py") if p.name != "__init__.py")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def slug(heading):
    """GitHub's anchor for a heading: lower case, markup and punctuation dropped, spaces to dashes."""
    h = re.sub(r"[`*_~]", "", heading.strip().lower())
    return re.sub(r"[^\w\- 　]", "", h).replace(" ", "-")


def anchors(rel):
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", read(rel), re.M)}


def help_options(*argv, capsys):
    try:
        hermind_cli.main([*argv, "--help"])
    except SystemExit:
        pass
    return set(re.findall(r"^\s{2}(--[a-z-]+)", capsys.readouterr().out, re.M)) - {"--help"}


@pytest.mark.parametrize("rel", CHANGED)
def test_every_relative_link_points_at_an_existing_file_and_heading(rel):
    text = re.sub(r"```.*?```", "", read(rel), flags=re.S)
    for m in re.finditer(r"\]\(([^)\s]+)\)", text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:", "../../")):   # "../../issues" is a GitHub-relative link
            continue
        path, _, frag = target.partition("#")
        dest = (ROOT / rel).parent / path if path else ROOT / rel
        assert dest.resolve().exists(), (rel, target, "no such file")
        if frag and dest.suffix == ".md":
            assert frag in anchors(dest.resolve().relative_to(ROOT).as_posix()), (rel, target, "no such heading")


def test_docs_holds_exactly_the_five_english_pages():
    files = {p.name for p in (ROOT / "docs").iterdir()}
    assert files == DOCS, sorted(files ^ DOCS)
    assert not list((ROOT / "docs").glob("*.zh-TW.md"))


def test_the_expected_headings_exist():
    wanted = {"docs/setup.md": ("MCP clients", "Hermes plugin", "Semantic search", "Optional LLM tools"),
              "docs/privacy.md": ("Encryption",), "docs/how-it-works.md": ("How well does it recall",),
              "docs/import-export.md": ("Import", "Export", "Backup")}
    for rel, headings in wanted.items():
        for h in headings:
            assert re.search(rf"^## {re.escape(h)}\s*$", read(rel), re.M), (rel, h)


@pytest.mark.parametrize("rel", sorted(f"docs/{n}" for n in DOCS))
def test_each_page_opens_with_a_sentence_and_a_contents_list(rel):
    lines = read(rel).splitlines()
    assert lines[0].startswith("# ") and lines[1] == "" and lines[2], rel
    assert any(l.startswith("- [") for l in lines[3:12]), rel


def test_the_import_page_names_every_outcome_option_and_reader(capsys):
    page = read(PAGE)
    for outcome in import_core.OUTCOMES:
        assert f"`{outcome}`" in page, outcome
    for opt in help_options("import", capsys=capsys):
        assert f"`{opt}" in page, opt
    for r in import_core.reader_list():
        assert re.search(rf"[(]`{r['name']}`[)]", page), r["name"]
    for word in ("--apply", "--list", "--revert", "--readers", "--include-removed", "AGENTS.md", "CLAUDE.md"):
        assert word in page
    assert page.index("`new`") < page.index("`version`") < page.index("`duplicate`")


def test_the_page_says_the_export_formats_are_third_party_descriptions_for_both_exports():
    page = read(PAGE)
    sentence = "Hermind reads these exports as third-party tools describe them; the formats are not published"
    assert page.count(sentence) == 2
    assert "Settings → Data controls → Export data" in page and "Settings → Privacy → Export data" in page


def test_the_page_has_every_field_of_the_json_record_and_every_export_option(capsys):
    import tempfile
    sys.path.insert(0, str(ROOT / "tests"))
    from export_fixture import build_db
    from store import DecisionStore
    import export_notes
    with tempfile.TemporaryDirectory() as d:
        build_db(Path(d) / "a.db")
        s = DecisionStore(str(Path(d) / "a.db"))
        try:
            keys = list(export_notes.collect_records(s.conn)[0])
        finally:
            s.close()
    page = read(PAGE)
    for k in keys:
        assert re.search(rf"^\| `{k}` \|", page, re.M), k
    for opt in help_options("export", capsys=capsys):
        assert opt in page, opt
    for word in ("jsonl", "markdown", "tools/backup.py", "hermind-note-", "Vectors"):
        assert word in page


def test_the_backup_section_names_the_options_of_tools_backup():
    section = read(PAGE).split("## Backup", 1)[1]
    for opt in ("--keep", "--dest"):
        assert opt in section


def test_the_two_readme_files_agree_about_what_export_writes():
    for rel in ("README.md", "README.zh-TW.md"):
        text = read(rel)
        assert "hermind export` writes Markdown" not in text and "hermind export` 可輸出 Markdown |" not in text
        assert "docs/import-export" in text
        assert re.search(r"hermind export --out notes\.json", text)


@pytest.mark.parametrize("rel", ["docs/import-export.md", "docs/how-it-works.md"])
def test_the_pages_name_no_other_memory_product(rel):
    text = read(rel).lower()
    for name in OTHER_PRODUCTS:
        assert not re.search(rf"\b{name}\b", text), (rel, name)


def test_the_traditional_chinese_readme_uses_no_simplified_characters():
    simplified = set("导记档库项结设为这个们说来时过删览继续对单发现处还经压缩读资选类别级检测订阅员码页进动态网络应认证书传输误务专业体从与关将开启关闭刚给让后见贝车长门问间题队适样户钟联讯录仅岁则点热爱虑决议论该请谢调转钮击确译约络纳细终组织绝统维护显标签积极脑错权险试")
    assert not (set(read("README.zh-TW.md")) & simplified), sorted(set(read("README.zh-TW.md")) & simplified)


@pytest.mark.parametrize("module", RUNTIME_MODULES)
def test_the_module_table_has_a_row_for_every_runtime_module(module):
    assert re.search(rf"^\| `{re.escape(module)}` \|", read("docs/how-it-works.md"), re.M), module
