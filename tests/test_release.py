"""Release hygiene: one version, one skill, installable skill, packaged data."""

import re
from pathlib import Path

from typer.testing import CliRunner

import harnessdoc
from harnessdoc.cli import app

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    # regex, not tomllib: tomllib is 3.11+ and we support 3.10
    declared = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE).group(1)
    assert declared == harnessdoc.__version__
    assert CliRunner().invoke(app, ["version"]).output.strip() == harnessdoc.__version__


def test_repo_and_packaged_skill_are_identical():
    # skills/ is for people cloning the repo; src/harnessdoc/skill/ is what pip ships. They must not drift.
    repo = (ROOT / "skills" / "harnessdoc" / "SKILL.md").read_text()
    packaged = (ROOT / "src" / "harnessdoc" / "skill" / "SKILL.md").read_text()
    assert repo == packaged
    assert repo.startswith("---\nname: harnessdoc\n")


def test_skill_install(tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    target = tmp_path / "harnessdoc" / "SKILL.md"
    assert target.read_text() == (ROOT / "skills" / "harnessdoc" / "SKILL.md").read_text()

    assert "up to date" in runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)]).output

    target.write_text("my local edits")
    blocked = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])
    assert blocked.exit_code == 1 and target.read_text() == "my local edits"  # never silently overwritten
    assert runner.invoke(app, ["skill", "install", "--dir", str(tmp_path), "--force"]).exit_code == 0
    assert target.read_text().startswith("---\nname: harnessdoc")


def test_price_book_is_package_data():
    from importlib import resources

    assert resources.files("harnessdoc.data").joinpath("pricing.json").is_file()
    assert resources.files("harnessdoc").joinpath("skill/SKILL.md").is_file()
