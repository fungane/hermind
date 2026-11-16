"""Project identity: normalising a working directory, neutral directories, and matching two projects."""
import ntpath
import os
import pathlib
import sys

import pytest

import project
from project import ProjectMatcher, normalize, project_for

WIN = ntpath
HOME = "/home/tester"


# -- normalize ------------------------------------------------------------------------------------------------

def test_normalize_drops_a_trailing_separator_and_dot_segments():
    assert normalize("/srv/work/atlas/") == normalize("/srv/work/atlas") == os.path.normcase(os.path.abspath("/srv/work/atlas"))
    assert normalize("/srv/work/x/../atlas//") == normalize("/srv/work/atlas")


def test_normalize_accepts_a_path_object():
    assert normalize(pathlib.PurePath("/srv/work/atlas")) == normalize("/srv/work/atlas")


@pytest.mark.parametrize("value", [None, "", "   ", 5, b"/srv/work", [], "/srv/\x00bad"])
def test_normalize_gives_nothing_for_an_unusable_value(value):
    assert normalize(value) is None


def test_normalize_gives_nothing_for_a_filesystem_root():
    assert normalize("/") is None
    assert normalize("//") is None


def test_windows_spelling_folds_case_and_separators():
    a = normalize("C:\\Work\\Atlas\\", pm=WIN)
    assert a == "c:\\work\\atlas"
    assert normalize("c:/WORK/atlas", pm=WIN) == a
    assert normalize("C:\\Work\\Atlas\\.\\", pm=WIN) == a


def test_windows_roots_are_neutral():
    assert normalize("C:\\", pm=WIN) is None
    assert normalize("c:/", pm=WIN) is None
    assert normalize("\\\\host\\share\\", pm=WIN) is None
    assert normalize("\\\\host\\share\\atlas", pm=WIN) == "\\\\host\\share\\atlas"


@pytest.mark.skipif(os.name != "nt", reason="case folding of os.path is Windows-only")
def test_native_windows_case_folding():
    assert normalize("C:\\Work\\ATLAS") == normalize("c:\\work\\atlas\\")


@pytest.mark.skipif(os.name == "nt", reason="POSIX paths are case-sensitive")
def test_posix_paths_keep_their_case():
    assert normalize("/srv/Work") != normalize("/srv/work")


# -- matches --------------------------------------------------------------------------------------------------

def test_equal_and_nested_projects_match_in_both_directions():
    m = ProjectMatcher()
    assert m.matches("/srv/work/atlas", "/srv/work/atlas")
    assert m.matches("/srv/work/atlas", "/srv/work/atlas/services/api")
    assert m.matches("/srv/work/atlas/services/api", "/srv/work/atlas")


def test_a_bare_string_prefix_is_not_a_match():
    m = ProjectMatcher()
    assert not m.matches("/a/foo", "/a/foobar")
    assert not m.matches("/a/foobar", "/a/foo")
    assert not m.matches("/a/foo", "/a/foobar/x")


def test_unrelated_projects_do_not_match():
    assert not ProjectMatcher().matches("/srv/work/atlas", "/srv/work/borealis")


def test_an_unknown_side_matches_everything():
    m = ProjectMatcher()
    assert m.matches(None, "/srv/work/atlas") and m.matches("/srv/work/atlas", None) and m.matches(None, None)
    assert m.matches("", "/srv/work/atlas")


def test_windows_projects_compare_after_case_folding_and_never_across_drives():
    m = ProjectMatcher(pm=WIN)
    a, b = normalize("C:\\Work\\Atlas", pm=WIN), normalize("c:/work/ATLAS/api", pm=WIN)
    assert m.matches(a, b)
    assert not m.matches(a, normalize("D:\\Work\\Atlas", pm=WIN))
    assert not m.matches(normalize("C:\\a\\foo", pm=WIN), normalize("C:\\a\\foobar", pm=WIN))


def test_a_symlink_to_a_project_matches_it_by_real_path(tmp_path):
    real = tmp_path / "real" / "atlas"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    a, b = normalize(str(real)), normalize(str(link))
    assert a != b
    assert ProjectMatcher().matches(a, b)
    assert ProjectMatcher().matches(b, normalize(str(real / "src")))
    assert not ProjectMatcher().matches(b, normalize(str(tmp_path / "other")))


class CountingPm:
    """os.path that records every realpath call."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return getattr(os.path, name)

    def realpath(self, path, *a, **k):
        self.calls.append(path)
        return os.path.realpath(path, *a, **k)


def test_each_real_path_is_computed_once_per_distinct_value():
    pm = CountingPm()
    m = ProjectMatcher(pm=pm)
    a, b, c = "/srv/work/atlas", "/srv/work/borealis", "/srv/work/cirrus"
    for _ in range(3):
        assert not m.matches(a, b)
    assert not m.matches(a, c)
    assert not m.matches(b, c)
    assert sorted(pm.calls) == sorted([a, b, c])
    m.matches(a, a + "/src")                  # related by name: no real path needed
    assert len(pm.calls) == 3


# -- project_for ----------------------------------------------------------------------------------------------

def test_a_normal_directory_is_its_own_project():
    assert project_for("/srv/work/atlas/", home=HOME) == normalize("/srv/work/atlas")


@pytest.mark.parametrize("cwd", [None, "", "/"])
def test_no_directory_and_the_root_are_neutral(cwd):
    assert project_for(cwd, home=HOME) is None


def test_the_home_directory_is_neutral_but_a_folder_in_it_is_not():
    assert project_for(HOME, home=HOME) is None
    assert project_for(HOME + "/", home=HOME) is None
    assert project_for(HOME + "/atlas", home=HOME) == normalize(HOME + "/atlas")


def test_home_defaults_to_path_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert project_for(str(tmp_path)) is None
    assert project_for(str(tmp_path / "atlas")) == normalize(str(tmp_path / "atlas"))


def test_a_strict_ancestor_of_two_stored_projects_is_neutral():
    known = ["/srv/work/atlas", "/srv/work/borealis"]
    assert project_for("/srv/work", known, home=HOME) is None
    assert project_for("/srv/work/", known, home=HOME) is None
    assert project_for("/srv/work/atlas", known, home=HOME) == normalize("/srv/work/atlas")


def test_one_project_below_is_not_an_umbrella():
    assert project_for("/srv/work", ["/srv/work/atlas"], home=HOME) == normalize("/srv/work")
    assert project_for("/srv/work", ["/srv/work/atlas", "/srv/work/atlas"], home=HOME) == normalize("/srv/work")
    assert project_for("/srv/work", ["/srv/work", "/srv/work/atlas"], home=HOME) == normalize("/srv/work")


def test_the_umbrella_rule_compares_components_not_prefixes():
    assert project_for("/a/foo", ["/a/foobar/x", "/a/foobar/y"], home=HOME) == normalize("/a/foo")


def test_known_projects_that_are_already_normalised_give_the_same_answer():
    raw = ["/srv/work/atlas/", "/srv/work/borealis/../borealis"]
    clean = {normalize(p) for p in raw}
    assert project_for("/srv/work", raw, home=HOME) is None
    assert project_for("/srv/work", clean, home=HOME, normalized=True) is None
    assert project_for("/srv/work/atlas", raw, home=HOME) == project_for("/srv/work/atlas", clean, home=HOME, normalized=True)


def test_the_stored_projects_are_asked_only_when_they_matter():
    def boom():
        raise AssertionError("not needed")
    assert project_for(HOME, boom, home=HOME) is None
    assert project_for(None, boom, home=HOME) is None
    assert project_for("/srv/work", lambda: ["/srv/work/a", "/srv/work/b"], home=HOME) is None


def test_windows_umbrella_and_home_after_case_folding():
    known = ["C:\\Work\\Atlas", "c:\\work\\borealis"]
    assert project_for("C:\\WORK", known, home="C:\\Users\\Tester", pm=WIN) is None
    assert project_for("c:\\users\\TESTER\\", [], home="C:\\Users\\Tester", pm=WIN) is None
    assert project_for("C:\\Work\\Atlas", known, home="C:\\Users\\Tester", pm=WIN) == "c:\\work\\atlas"
