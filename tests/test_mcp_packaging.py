"""pyproject sanity (the real wheel build / install check needs network and is done by hand, see REPORT.md)."""

import re
import pytest
tomllib = pytest.importorskip("tomllib")   # Python 3.11+
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "hermind"


def test_pyproject_has_no_runtime_dependencies_and_the_console_script():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    proj = cfg["project"]
    assert proj["dependencies"] == [] and proj["requires-python"] == ">=3.10"
    assert proj["scripts"] == {"hermind-mcp": "hermind.hermind_mcp:main", "hermind-hook": "hermind.hermind_hook:main",
                              "hermind": "hermind.hermind_cli:main"}
    assert cfg["tool"]["setuptools"]["packages"] == ["hermind"] and "package-dir" not in cfg["tool"]["setuptools"]
    assert (REPO / "hermind" / "__init__.py").exists()            # the package folder is hermind/, beside pyproject.toml
    assert "synonyms.yaml" in cfg["tool"]["setuptools"]["package-data"]["hermind"]
    assert re.search(r"^def main\(", (ROOT / "hermind_mcp.py").read_text(), re.M)


def test_the_only_extra_is_encrypted_and_keywords_are_project_metadata():
    """A key written under [project.optional-dependencies] by mistake becomes an extra that installs strangers' packages."""
    proj = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert proj["optional-dependencies"] == {"encrypted": ["sqlcipher3"]}
    assert proj["keywords"] and proj["authors"] and proj["urls"]["Homepage"].startswith("https://github.com/")


def test_plugin_yaml_has_a_version_and_deploy_script_does_not_ship_the_mcp_server():
    assert re.search(r"^version: \d+\.\d+\.\d+$", (ROOT / "plugin.yaml").read_text(), re.M)
    deploy = (ROOT / "tools" / "deploy.sh").read_text()
    assert "hermind_mcp" not in deploy and "pyproject" not in deploy
