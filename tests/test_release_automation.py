"""The release tooling: the pre-release check script and the safety properties of the release workflow."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_release", ROOT / "scripts" / "check_release.py")
check_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_release)


def make_repo(tmp_path: Path, pyproject="0.2.0", init="0.2.0", changelog="## [0.2.0] - 2026-12-01\n\n- a change\n"):
    (tmp_path / "src" / "harnessdoc").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{pyproject}"\n')
    (tmp_path / "src" / "harnessdoc" / "__init__.py").write_text(f'__version__ = "{init}"\n')
    if changelog is not None:
        (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## [Unreleased]\n\n{changelog}")
    return tmp_path


def test_good_release_passes(tmp_path):
    assert check_release.problems("v0.2.0", make_repo(tmp_path)) == []


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v1", "release-0.2.0", "v0.2.0.1", "vlatest"])
def test_malformed_tags_are_rejected(tmp_path, tag):
    assert "not of the form" in check_release.problems(tag, make_repo(tmp_path))[0]


def test_prerelease_tags_are_accepted_when_everything_matches(tmp_path):
    repo = make_repo(tmp_path, "0.3.0rc1", "0.3.0rc1", "## [0.3.0rc1] - 2026-12-01\n\n- candidate\n")
    assert check_release.problems("v0.3.0rc1", repo) == []


def test_version_mismatches_are_each_reported(tmp_path):
    errors = check_release.problems("v0.2.0", make_repo(tmp_path, pyproject="0.1.9", init="0.1.8"))
    assert len(errors) == 2 and "pyproject.toml" in errors[0] and "__init__.py" in errors[1]


def test_changelog_must_have_a_non_empty_section_for_the_version(tmp_path):
    assert "no '## [0.2.0]' section" in check_release.problems(
        "v0.2.0", make_repo(tmp_path, changelog="## [0.1.0] - 2026-11-10\n\n- old\n"))[0]
    assert "empty" in check_release.problems(
        "v0.2.0", make_repo(tmp_path / "e", changelog="## [0.2.0] - 2026-12-01\n\n## [0.1.0]\n\n- old\n"))[0]
    assert "CHANGELOG.md is missing" in check_release.problems("v0.2.0", make_repo(tmp_path / "m", changelog=None))[0]


def test_changelog_section_extraction_stops_at_the_next_version():
    text = "## [Unreleased]\n\n## [0.2.0] - d\n\nnew\n\n### Fixed\n- x\n\n## [0.1.0] - d\n\nold\n"
    assert check_release.changelog_section("0.2.0", text) == "new\n\n### Fixed\n- x"
    assert check_release.changelog_section("0.1.0", text) == "old"
    assert check_release.changelog_section("9.9.9", text) is None


def test_the_repos_own_state_passes_for_its_current_version():
    import harnessdoc

    assert check_release.problems(f"v{harnessdoc.__version__}") == []  # CHANGELOG has this version's section


# ----------------------------------------------------------------------------- the workflow's safety properties


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())


def test_release_only_runs_on_version_tags(workflow):
    triggers = workflow.get(True) or workflow["on"]  # YAML parses a bare `on:` key as boolean True
    assert triggers == {"push": {"tags": ["v*"]}}


def test_publish_needs_the_build_and_a_protected_environment(workflow):
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["environment"]["name"] == "pypi"  # the environment holds the required-reviewer approval
    assert publish["permissions"] == {"id-token": "write"}  # Trusted Publishing; nothing else


def test_no_stored_pypi_token_anywhere(workflow):
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "PYPI_TOKEN" not in text and "password:" not in text and "UV_PUBLISH_TOKEN" not in text


def test_default_permissions_are_read_only_and_release_job_alone_can_write(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    writers = [name for name, job in workflow["jobs"].items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["github-release"] and workflow["jobs"]["github-release"]["needs"] == "publish"


def test_build_runs_the_checks_before_building(workflow):
    steps = [s.get("name", s.get("uses", "")) for s in workflow["jobs"]["build"]["steps"]]
    order = [next(i for i, s in enumerate(steps) if key in s) for key in ("Check the tag", "Tests", "Build sdist")]
    assert order == sorted(order)
