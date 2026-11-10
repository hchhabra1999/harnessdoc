# Releasing harnessdoc

A release = a version bump merged to `main`, then a tag. Pushing the tag runs
[`.github/workflows/release.yml`](.github/workflows/release.yml), which checks, tests, builds, waits for **your
approval**, publishes to PyPI and creates the GitHub release. **A PyPI upload is permanent** (a version number can
never be reused or replaced), so the approval click is deliberate.

## One-time setup (needed before the first automated release; repeat only if the project/repo is renamed or recreated)

1. **PyPI Trusted Publisher** — pypi.org → project *harnessdoc* → *Manage* → *Publishing* → *Add a new publisher*
   → GitHub → owner `hchhabra1999`, repository `harnessdoc`, workflow `release.yml`, environment `pypi`.
   (This replaces API tokens entirely — none are stored in GitHub or on your machine. Once it works, delete any
   account-wide PyPI token.)
2. **GitHub environment `pypi`** — repo *Settings → Environments → pypi* with **Required reviewers = you**.
   This is what makes the publish job pause for your click.

## Every release

Use [semver](https://semver.org/) (0.x: minor bumps may break things): `0.1.1` fix · `0.2.0` feature · `1.0.0` stable.

1. **Branch:** `git switch main && git pull && git switch -c release-X.Y.Z`
2. **Bump the version in both places** (a test fails if they differ):
   `pyproject.toml` → `version = "X.Y.Z"` and `src/harnessdoc/__init__.py` → `__version__ = "X.Y.Z"`.
3. **Changelog:** in `CHANGELOG.md` turn `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` (and add a fresh empty
   `## [Unreleased]` above it); update the two compare links at the bottom. This section becomes the GitHub release
   notes, so write it for users, including any new known limits.
4. **Check locally:**
   ```
   python scripts/check_release.py vX.Y.Z    # tag/version/changelog agree
   pytest -q && ruff check src tests scripts examples/demo_app/logs
   uv build && twine check dist/*            # optional dry run; dist/ is git-ignored
   ```
5. **Open a PR** `release-X.Y.Z` → `main`. CI must be green. Branch protection needs a code-owner review; as the
   repo owner you can't approve your own PR, so merge it using the admin bypass ("Merge without waiting for
   requirements") or push to `main` directly — the owner is the one exception to the review rule.
6. **Tag the merge commit and push the tag:**
   ```
   git switch main && git pull
   git tag -a vX.Y.Z -m "harnessdoc X.Y.Z"
   git push origin vX.Y.Z
   ```
7. **Approve the publish:** GitHub → *Actions* → the `release` run → when the `publish` job shows "Waiting",
   click **Review deployments → pypi → Approve and deploy**. (If `build` failed, nothing was published — see below.)
8. **Verify:**
   ```
   python -m venv /tmp/v && /tmp/v/bin/pip install "harnessdoc[mcp]==X.Y.Z" && /tmp/v/bin/harnessdoc version
   ```
   and check https://pypi.org/project/harnessdoc/ and the new GitHub release.

Pre-releases: tag `vX.Y.ZrcN` (or `aN`/`bN`) with a matching version and a `## [X.Y.ZrcN]` changelog section;
the GitHub release is marked pre-release, and pip won't install it unless asked.

## When something goes wrong

| Where it failed | Nothing published yet? | What to do |
|---|---|---|
| `build` job (check, lint, tests, build) | yes | Fix on a branch, merge. Move the tag: `git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`, then tag the new commit and push again. |
| You don't want to publish | yes | Click **Reject** on the deployment. Delete the tag as above. |
| `publish` job after approval | maybe | Check pypi.org for the version. If it's there, treat as published (next row). If not, re-run the failed job. |
| Published a bad release | no | **Never delete or re-upload.** On PyPI, *Yank* the release (pip then skips it), fix, and ship `X.Y.Z+1`. Say so in the changelog. |
| `github-release` job failed, PyPI is fine | no | Create it by hand: `gh release create vX.Y.Z --notes-file <(python scripts/check_release.py vX.Y.Z --notes)` |

## What the workflow checks (and why it can refuse)

`scripts/check_release.py` fails the run unless the tag is `vMAJOR.MINOR.PATCH`, both version strings equal it, and
`CHANGELOG.md` has a non-empty section for it. Then `ruff` and the full `pytest` run, the package is built and
`twine check`ed, and only then can the approval-gated publish run. Publishing needs no stored secret: PyPI trusts
this exact repo + workflow + environment via OIDC.
