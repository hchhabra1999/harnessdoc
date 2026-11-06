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

> Status: pre-alpha, week 9 of 10. What works today: the static scanner, the usage analyzer, the
> Markdown report, before/after comparison, guard-eval scaffolds, the CI gate, the experiment runner, the
> MCP server and the Claude Code skill. See the [PRD](docs/PRD.md) for the plan.

## Use it from your coding agent

harnessdoc ships an MCP server and a Claude Code skill. The agent interviews you about what the app is for,
then calls harnessdoc for every number — harnessdoc itself never calls an LLM.

```bash
pip install "harnessdoc[mcp]"                       # (from a clone for now: uv pip install -e ".[mcp]")

# Claude Code
claude mcp add harnessdoc -- harnessdoc mcp
cp -r skills/harnessdoc ~/.claude/skills/            # then type /harnessdoc in Claude Code

# Cursor / other MCP clients: add to mcp.json
#   {"mcpServers": {"harnessdoc": {"command": "harnessdoc", "args": ["mcp"]}}}
```

| MCP tool | Does | Writes files? |
|---|---|---|
| `scan` | LLM call sites, findings HD001–HD010, uncovered call sites | no |
| `measure` | Baseline from usage logs; each finding priced in $/month | no |
| `price` | Price token counts for any model in the price book | no |
| `explain_rule` | Why a rule matters, how to fix it, how its saving is estimated | no |
| `read_goals` / `write_goals` | The project's `harnessdoc.goals.yaml` | `write_goals` only |
| `write_report` | `harnessdoc-report.md` in the project | yes |
| `scaffold_guard` | Guard eval (pytest + cases file) for an uncovered call site; never overwrites | yes |
| `compare` | Before/after verdict: SHIP / REJECT / INCONCLUSIVE / NO CLEAR SAVING | no |
| `check` | What the CI gate would say: new vs fixed findings against the baseline | no |
| `plan_experiment` | Runs and estimated cost of an experiment; calls nothing | no |
| `run_experiment` | Runs it with your keys — needs `user_approved=true` and a budget | results file |

Paths are confined to the project directory you pass. The [skill](skills/harnessdoc/SKILL.md) adds the
workflow and its rules: quote only harnessdoc's numbers, label estimates, write a guard eval before
changing an unguarded call, and apply one approved change at a time.

## Prove the saving

```bash
harnessdoc guard scaffold extract_order_id          # guard eval for a call site that has none
harnessdoc compare --before logs/week44.jsonl --after logs/week45.jsonl
```

`compare` answers one question — *is each successful task cheaper now, without hurting quality?* — and is
built to say "not sure yet" when that's the truth:

- **Cost:** cost per successful task before vs after, with a 95% CI on the change (independent windows,
  delta method). LOWER only if the whole interval is below zero.
- **Quality:** a non-inferiority test on success rate (Newcombe score interval) against the allowed drop in
  `harnessdoc.goals.yaml`. PASS only if even the worst case stays within it.
- **Verdict:** SHIP (cheaper and quality held), REJECT (quality fell beyond the bar, or cost rose),
  INCONCLUSIVE (with how many tasks per window would settle it), or NO CLEAR SAVING.

On the demo app, applying the top recommendations gives:

```
Verdict: INCONCLUSIVE   (quality INCONCLUSIVE vs allowed −1 pt · cost LOWER)
  Cost per successful task   $0.0366 → $0.0214  (−41.6%, 95% CI of the change −$0.0188 to −$0.0117)
  Spend / 30 days            $107.94 → $41.23   · cache hit rate 0% → 41.7%
  Success rate +0.8 pt, but the CI (−3.7 to +5.3) still allows a drop beyond −1 pt.
  About 3,060 tasks per window would settle it.
```

The saving is real; whether quality held within one point isn't knowable from a week of 480 tickets — so it
doesn't claim it. With a 5-point bar the same data says SHIP.

Guard evals count toward coverage only once their cases file has at least one case, and a test covers an
LLM call when it calls any function that leads to it (handler → helper → LLM wrapper).

## Try a cheaper setup before changing code

```bash
harnessdoc experiment plan  harnessdoc.experiment.yaml            # runs + estimated cost; calls nothing
harnessdoc experiment run   harnessdoc.experiment.yaml --budget 0.50
```

An experiment replays a call site's guard-eval cases through **your real code** under several variants —
another model (`model_overrides`), a different output cap (`max_tokens`), extra request settings
(`kwargs`, e.g. `cache_control`), or environment flags your app reads (`env`) — with no code edits.
harnessdoc briefly intercepts the Anthropic / OpenAI / Gemini SDK call to apply the variant and record usage,
scores every answer, and gives each variant the same SHIP / REJECT / INCONCLUSIVE verdict as `compare`.

It is the only part of harnessdoc that costs money, so:

- it uses **your** API keys from the environment — harnessdoc never reads or stores them;
- `--budget` is a hard cap: no new LLM call starts once it is reached (one in-flight call can overshoot by its
  own cost), and the stop can't be swallowed by an app's `except Exception` retry loop;
- `run` shows the plan and asks before spending (unless `--yes`); over MCP, `run_experiment` refuses
  without `user_approved=true` and a budget;
- variants are interleaved case by case, so drift doesn't favour one of them; prompts and outputs are never
  written to disk.

The demo app ships one: [`harnessdoc.experiment.yaml`](examples/demo_app/harnessdoc.experiment.yaml) tests
the intent router on Haiku against its 24 labelled cases — 72 runs, estimated at about one cent.

## CI gate

```bash
harnessdoc check . --write-baseline harnessdoc-baseline.json   # once: today's findings become accepted debt
harnessdoc check . --baseline harnessdoc-baseline.json --fail-on medium   # every PR
```

Findings are matched by rule + file + enclosing function, not line number, so unrelated edits don't make
old findings look new; fixed findings are listed. A ready-made GitHub Actions workflow that also uploads the
report is in [`examples/ci/harnessdoc.yml`](examples/ci/harnessdoc.yml).

## Goals and the report

```bash
harnessdoc goals init .        # what the app does, what a successful task is, target, constraints, logs
harnessdoc report .            # → harnessdoc-report.md
```

The report states the goal, the baseline (cost per successful task vs target), recommendations ranked by
measured $/month with what to do first, which call sites need a guard eval before any change, every
finding in detail, and how each number was computed. It has no timestamp, so the same inputs produce the
same file and it can be diffed in a PR.

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

## Measure real usage

```bash
.venv/bin/harnessdoc measure logs/                          # spend, cache hit rate, cost per successful task
.venv/bin/harnessdoc measure logs/ --scan .                 # + every finding priced in measured $/month
.venv/bin/harnessdoc measure logs/ --outcomes outcomes.csv  # outcomes kept elsewhere (task_id,success)
```

`measure` reads the logs you already have, auto-detected line by line: raw Anthropic / OpenAI (Chat and
Responses) / Gemini responses dumped as JSONL (optionally wrapped with `task_id`, `site`, `ts`),
OpenTelemetry GenAI spans (flat or OTLP JSON), gzip files and whole folders. No logs yet? Drop in the
dependency-free logger — it writes token counts and ids only, never prompts or completions:

```python
from harnessdoc.log import UsageLogger
usage_log = UsageLogger("logs/llm_usage.jsonl")

response = client.messages.create(...)
usage_log.record(response, task_id=ticket.id)           # site defaults to the calling function
usage_log.outcome(ticket.id, success=ticket.resolved)   # once the task is over
```

**Cost per successful task** = everything spent on tasks with a known outcome ÷ number of successes, so the
spend on failed tasks counts against the successes that carry it. It comes with a 95% confidence interval
(delta method over tasks), next to the success rate (Wilson interval) and per-task mean/median/p90. Spend
that belongs to no task outcome (e.g. a nightly batch job) is reported separately, not hidden.

With `--scan`, each finding is priced from its call site's measured spend, and the combined total does not
double count: savings on the same call site combine as `1 − Π(1 − share)`.

Try it on the demo app with a synthetic week of traffic:

```bash
.venv/bin/python examples/demo_app/logs/generate_logs.py
.venv/bin/harnessdoc measure examples/demo_app/logs/usage.jsonl --scan examples/demo_app
# → cost per successful task $0.0366 (95% CI $0.0333–$0.0399) · combined potential ~$65 of ~$108 / month
```

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
| 4–5 | Usage analyzer → baseline cost per successful task, findings priced in measured $/month ✅ |
| 6 | MCP server + Claude Code skill, goals interview, report ✅ |
| 7–8 | Eval-coverage map, guard evals, before/after with confidence intervals, CI gate ✅ |
| 9 | Experiment runner (your key): replay samples on cheaper settings ✅ |
| 10 | Claude Code session logs, open-source case study, PyPI release |

## Licence

MIT
