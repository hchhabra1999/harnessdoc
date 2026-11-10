# Contributing to harnessdoc

Thanks for helping. A few rules keep the tool trustworthy — its whole job is giving numbers people can rely on.

## How changes get in

- **`main` is protected.** Nobody pushes to it directly except the maintainer. Fork the repo, branch, and open a
  pull request. Every PR needs a review from the code owner ([CODEOWNERS](.github/CODEOWNERS)) and a green CI.
- Keep PRs small and about one thing. Describe what changed and how you checked it.
- Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- Releases are made by the maintainer — see [RELEASING.md](RELEASING.md).

## Set up

```bash
git clone https://github.com/hchhabra1999/harnessdoc && cd harnessdoc
uv venv .venv && uv pip install -p .venv -e ".[dev]"      # or: python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts examples/demo_app/logs
```

CI runs the same on Python 3.10, 3.12 and 3.13, so don't use features newer than 3.10 (e.g. `tomllib`).

## Standards

- **Tests for every behaviour change.** New scanner check → a positive and a negative test, and the benchmark in
  `tests/test_benchmark_demo.py` must still be 10/10 with 0 false positives. Statistics → test against hand-computed
  or published values, not just "runs".
- **No invented numbers.** Prices come from the provider's own page with the source URL and date in
  `src/harnessdoc/data/pricing.json`; anything unconfirmed is marked `verify`. Savings are labelled *estimates*
  until a before/after run measures them.
- **Never log or store prompts, completions or API keys.** harnessdoc reads token counts and ids only.
- **Spending money needs consent.** Anything that calls an LLM API must show a plan, require a budget and stop
  when it can't price a call (see `experiment.py`).
- **Don't commit** generated logs, experiment results, reports or anything under `.claude/`.
- **Security issues:** please don't open a public issue — email the maintainer via the address on the GitHub
  profile, or use GitHub's private vulnerability reporting.
