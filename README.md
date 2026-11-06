# harnessdoc

**Measure what each successful task of your AI app costs — and prove when a change makes it cheaper without
making it worse.**

harnessdoc scans your code for every LLM call, reads your usage logs, and tells you — with `file:line`
evidence, a dollar estimate and the eval that guards quality — where your harness is wasting tokens. Then it
re-measures after you change something, so the saving is proven, not claimed.

- **Provider-neutral:** Anthropic, OpenAI, Gemini, LiteLLM.
- **Deterministic:** the same code and logs give the same numbers, so it can gate CI.
- **No LLM calls of its own:** use it as a CLI, in CI, as an MCP server (Claude Code, Cursor, Codex…) or as a
  Claude Code skill — your agent does the reasoning; harnessdoc does the measuring.

> Status: pre-alpha, week 3 of 10. What works today: the static scanner and the price book / cost
> engine. See the [PRD](docs/PRD.md) for the plan.

## Scan a project

```bash
uv venv .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/harnessdoc scan path/to/your/app            # readable report
.venv/bin/harnessdoc scan path/to/your/app --json     # stable JSON for tooling
.venv/bin/harnessdoc scan path/to/your/app --fail-on high   # exit 1 in CI if any high finding
```

The scanner parses your Python (it never imports or runs it), finds every LLM call made through the
Anthropic, OpenAI, Gemini or LiteLLM SDKs, resolves what goes into each call — model, output cap, system
prompt (followed across functions, constants, files and modules), tools, caching — and runs these checks:

| Rule | Finds | Saving estimate from |
|---|---|---|
| HD001 | Volatile values (`datetime.now()`, UUIDs…) at the start of a prompt that bust the cache | logs (later) |
| HD002 | A large static prefix sent to Anthropic without `cache_control` | price book: cache-read vs input |
| HD003 | Tool lists built by iterating a `set` (order differs per process → no shared cache) | logs (later) |
| HD004 | `while True` + `except: continue` retrying forever | — |
| HD005 | Agent loops with no iteration cap | — |
| HD006 | A premium model doing classification / extraction / short summaries | price book: small vs premium model |
| HD007 | `max_tokens` in the thousands for a one-word answer | — |
| HD008 | An LLM extracting something a regex can (IDs, emails, URLs) | 100% of the call when the regex matches |
| HD009 | Offline jobs calling the real-time API in a loop | price book: batch multiplier |
| HD010 | LLM calls no test/eval references (no quality guard) | — |

Every finding names the eval that guards the affected code, or says there is none. Savings are labelled
*estimates* until usage logs are joined (weeks 4–5).

**Benchmark:** on the [deliberately wasteful demo app](examples/demo_app) the scanner finds 10/10 planted
issues with 0 false positives (`tests/test_benchmark_demo.py`). That app was written by us, so the honest
test of precision is real open-source apps — that's the week-10 case study.

## Try the cost engine

```bash
.venv/bin/harnessdoc models --provider anthropic
.venv/bin/harnessdoc cost anthropic.claude-opus-5-5 --in 3000 --cache-read 6000 --out 400
.venv/bin/harnessdoc cost gpt-6.1-sol --cache-write 2000 --ttl 12h --json
```

Model ids are resolved the way they appear in real code: Bedrock/LiteLLM/Vertex prefixes, dated snapshots and
`-latest` suffixes all map to the right price.

## Prices

`src/harnessdoc/data/pricing.json` holds prices (USD per 1M tokens) and caching rules for Anthropic, OpenAI
and Gemini, each with its source URL and an `as_of` date, plus scheduled price changes and long-context
tiers. Providers change these often — override or add models per project with a `harnessdoc.pricing.json`
in your repo root (deep-merged over the defaults).

## Benchmark: the wasteful demo app

[`examples/demo_app`](examples/demo_app) is a small support bot with 11 planted kinds of harness waste
(cache-busting timestamps, non-deterministic tool order, an Opus-tier model for classification, an LLM doing
a regex's job, a real-time API for a nightly batch, uncovered call sites…). The [answer key](examples/demo_app/WASTE.md)
is how the scanner's recall and false-positive rate are measured.

## Roadmap

| Week | Ships |
|---|---|
| 1 | Price book + cost engine (3 providers), demo app + answer key ✅ |
| 2–3 | Python scanner: LLM call sites, checks HD001–HD010, eval coverage, CI exit codes ✅ |
| 4–5 | Usage analyzer → baseline cost per successful task |
| 6 | MCP server + Claude Code skill, goals interview, report |
| 7–8 | Eval-coverage map, guard evals, before/after with confidence intervals, CI gate |
| 9 | Experiment runner (your key): replay samples on cheaper settings |
| 10 | Claude Code session logs, open-source case study, PyPI release |

## Licence

MIT
