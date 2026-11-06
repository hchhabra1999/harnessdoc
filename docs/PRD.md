# harnessdoc — one-page PRD

| | |
|---|---|
| Owner | Harsh Chhabra |
| Status | v0.1 in build (week 1 of 10) |
| Updated | 2026-11-06 |

## What it is (one paragraph)

harnessdoc is a Python package that measures what each **successful task** of an AI app costs, and proves
when a change makes it cheaper without making it worse. It scans the app's code for every LLM call (model,
prompt size, caching, tools, retries, loops, eval coverage), reads the app's usage logs to get real token and
dollar numbers, and produces a ranked list of improvements — each with `file:line` evidence, an estimated
saving and the eval that guards quality. After a change it re-measures and reports the before/after with a
confidence interval. It is **deterministic** (same input → same numbers, so it can gate CI), **provider-neutral**
(Anthropic, OpenAI, Gemini, LiteLLM) and makes **no LLM calls itself**: it ships as a CLI, an MCP server and a
Claude Code skill, so the user's own agent does the reasoning and conversation on top of harnessdoc's numbers.

## What the user sees

*Illustrative output on the demo app — the numbers are placeholders until the usage analyzer (week 4) measures them.*

```
$ harnessdoc review .          # or /harnessdoc in Claude Code, or the MCP tool from Cursor
Goal: support bot · success = ticket resolved without human · 40K conversations/month

Baseline (from logs, 7 days)      $0.042 per successful task   (n=3,912, 95% CI ±$0.002)
Call sites                         4 found · 1 covered by an eval

 #  Finding                                         Where                       Est. saving   Guard
 1  Timestamp at start of system prompt busts cache acme_support/agent.py:15    −31% $/task   none → write one
 2  Static 1.4K-token prefix sent uncached          acme_support/agent.py:26    (with #1)
 3  Real-time API for nightly batch job             jobs/nightly_summary.py:10  −50% of job   none
 4  Regex-able extraction done by an LLM            acme_support/extract.py:9   −100% of site none
 5  Opus-tier model for 6-label classification      acme_support/router.py:12   −97% of site  ✓ test_router
```

## Who it's for

- **AI engineers** at startups/scale-ups whose LLM bill is growing faster than usage, across more than one provider.
- **Tech leads / PMs** who need a number they can defend: "cost per resolved ticket went from $0.042 to $0.026,
  quality unchanged" — not a vibe.

## Why now / why us

- "Harness engineering" is the 2026 theme (OpenAI, LangChain, Anthropic posts); harness choice moves cost far
  more than it moves success rate.
- GitHub's engineering blog reduced agent token usage 43–62% by hand with exactly this method (log usage →
  change harness → compare before/after). Nobody ships it as a tool.

## Positioning

| | Static token linters (tokenlint, llmdoctor, pcdlint) | Anthropic `/claude-api cost-optimize` | **harnessdoc** |
|---|---|---|---|
| Providers | mostly multi | Claude only | Anthropic, OpenAI, Gemini, LiteLLM |
| Numbers | guessed from code | model reads code/logs; varies per run | measured from logs; deterministic |
| Goal / success definition | no | yes (interactive) | yes (`goals.yaml`, reused every run) |
| Eval coverage map | no | builds evals per request | maps every call site; flags uncovered |
| Before/after proof | no | keep/revert per eval | CI-ready comparison with confidence intervals |
| Where it runs | CLI / plugin | Claude Code | CLI, CI, MCP (any agent), Claude Code skill |

We complement Anthropic's workflow (its agent can call harnessdoc's MCP tools for the numbers) rather than
compete with it.

## Scope v0.1 (10 weeks)

| Week | Ships |
|---|---|
| 1 | Package, pricing & caching rules as data (3 providers), demo app + answer key ✅ |
| 2–3 | Python scanner: call sites for Anthropic/OpenAI/Gemini/LiteLLM SDKs; first checks for W01–W10 |
| 4–5 | Usage analyzer (provider usage JSONL, OTel, LiteLLM logs) → baseline cost per successful task |
| 6 | MCP server + Claude Code skill; `goals.yaml` interview; Markdown/JSON report |
| 7–8 | Eval-coverage map, guard-eval scaffolds, before/after comparison with CIs, `--fail-if` CI gate |
| 9 | Experiment runner (user's key): replay samples on a cheaper model / shorter prompt, score with their evals |
| 10 | Claude Code session-log module, case study on 2–3 open-source AI apps, PyPI release |

Out of scope for v0.1: TypeScript scanning, a web UI, hosted service, automatic code rewriting without approval.

## Success metrics

| Metric | Target |
|---|---|
| Scanner on demo answer key | recall ≥ 9/10 planted issues, ≤ 1 false positive |
| Determinism | identical report on repeated runs (byte-for-byte JSON) |
| Case study | measured ≥ 25% lower cost per successful task on at least one real open-source app, quality held |
| Adoption (90 days) | 5 external repos run it; 3 written feedback quotes; 100+ GitHub stars |

## Risks

| Risk | Mitigation |
|---|---|
| Provider prices/caching rules change | Data-driven price book with `as_of` + per-project override file |
| Static analysis misses dynamic patterns (prompts built at runtime) | Join with logs; report "unresolved" sites instead of guessing |
| Anthropic/OpenAI ship the same thing natively | Stay provider-neutral and CI-first; integrate via MCP |
| Savings claims without proof | Every saving labelled *estimated* until a before/after run measures it |
