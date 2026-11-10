"""Pre-release checks, run by .github/workflows/release.yml and runnable by hand.

    python scripts/check_release.py v0.2.0            # verify the tag matches the code and the changelog
    python scripts/check_release.py v0.2.0 --notes    # print that version's CHANGELOG section (release notes)

Fails (exit 1) unless ALL of these hold:
  * the tag looks like vMAJOR.MINOR.PATCH (optionally with a pre-release suffix, e.g. v0.2.0rc1)
  * pyproject.toml's version and src/harnessdoc/__init__.py's __version__ both equal the tag without the "v"
  * CHANGELOG.md has a "## [<version>]" section with some content under it
Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$")


def _find(pattern: str, path: Path) -> str | None:
    m = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    return m.group(1) if m else None


def changelog_section(version: str, text: str) -> str | None:
    """The body under `## [version]` up to the next `## [` heading, or None if there is no such section."""
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else None


def problems(tag: str, root: Path = ROOT) -> list[str]:
    found = []
    m = TAG_RE.match(tag)
    if not m:
        return [f"tag {tag!r} is not of the form vMAJOR.MINOR.PATCH (e.g. v0.2.0)"]
    version = m.group(1)
    declared = _find(r'^version = "([^"]+)"', root / "pyproject.toml")
    in_code = _find(r'^__version__ = "([^"]+)"', root / "src" / "harnessdoc" / "__init__.py")
    if declared != version:
        found.append(f"pyproject.toml says version {declared!r}, tag says {version!r}")
    if in_code != version:
        found.append(f"src/harnessdoc/__init__.py says __version__ {in_code!r}, tag says {version!r}")
    changelog = root / "CHANGELOG.md"
    if not changelog.exists():
        found.append("CHANGELOG.md is missing")
    else:
        body = changelog_section(version, changelog.read_text(encoding="utf-8"))
        if body is None:
            found.append(f"CHANGELOG.md has no '## [{version}]' section")
        elif not body:
            found.append(f"CHANGELOG.md's '## [{version}]' section is empty")
    return found


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    tag = args[0]
    errors = problems(tag)
    if errors:
        print("Release check FAILED:", *errors, sep="\n  - ")
        return 1
    if "--notes" in argv:
        print(changelog_section(TAG_RE.match(tag).group(1), (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))
    else:
        print(f"Release check passed for {tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
