# Changelog

All notable changes to harnessdoc. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/) (0.x: minor versions may break things).

Each release's section is used verbatim as its GitHub release notes, so write it for users.

## [Unreleased]

## [0.1.0] - 2026-11-10

First public release (alpha).

### Added
- Static scanner for the Anthropic, OpenAI, Gemini and LiteLLM SDKs with checks HD001–HD010, `file:line`
  evidence, eval-coverage mapping and CI exit codes (`harnessdoc scan`).
- Usage analyzer: cost per successful task with a 95% CI, findings priced in measured $/month
  (`harnessdoc measure`, `harnessdoc.log.UsageLogger`).
- Before/after comparison with a quality non-inferiority test: SHIP / REJECT / INCONCLUSIVE / NO CLEAR SAVING
  (`harnessdoc compare`).
- Guard-eval scaffolds, the new-findings-only CI gate, and an experiment runner with a hard budget that stops
  when a call can't be priced.
- MCP server (12 tools) and the `/harnessdoc` Claude Code skill (`harnessdoc skill install`).
- `harnessdoc sessions`: what your Claude Code usage costs at API list prices (token counts only).
- Price book for Anthropic, OpenAI, Gemini and MiniMax, each entry dated with its source.

### Known limits
- Python only; framework-wrapped calls (e.g. LangChain) are not followed.
- Models chosen at runtime are invisible to the static scanner.
- No measured saving on a real app yet; demo numbers are synthetic.
- `sessions` dollars are API-equivalent, not what a subscription costs.

[Unreleased]: https://github.com/hchhabra1999/harnessdoc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hchhabra1999/harnessdoc/releases/tag/v0.1.0
