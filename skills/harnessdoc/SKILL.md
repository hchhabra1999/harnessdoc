---
name: harnessdoc
description: Review an AI app's harness for wasted LLM spend and prove savings. Use when the user asks to reduce LLM/token costs, audit how their app calls OpenAI/Anthropic/Gemini/LiteLLM, find out what each successful task costs, check prompt caching, or "run harnessdoc". Interviews the user about goals, scans the code, measures usage logs, writes a ranked report, and applies fixes one at a time only with approval.
---

# harnessdoc — harness review with measured savings

harnessdoc is a deterministic measuring tool; **you** do the conversation and the reasoning. Use its MCP
tools when connected (`scan`, `measure`, `price`, `explain_rule`, `read_goals`, `write_goals`,
`write_report`), otherwise the CLI (`harnessdoc scan|measure|report|goals …`). If neither is available, tell
the user to `pip install "harnessdoc[mcp]"` and add the server with `claude mcp add harnessdoc -- harnessdoc mcp`.

## Non-negotiable rules

1. **Only quote numbers harnessdoc returned.** Never estimate a dollar figure, token count or percentage
   yourself. If a number isn't in the tool output, say it isn't measured.
2. **Label estimates.** Savings are estimates until a before/after run measures them — say so.
3. **Guard before you change quality.** Never change a call site's model, prompt or replace it with code while
   its finding shows `guard: []`. Offer to write a small guard eval first (10–30 real inputs with expected
   outputs, in the project's existing test framework).
4. **One change at a time, with approval.** Show the diff, wait for a yes, apply, run the guard eval, then move
   to the next. Never batch unrelated changes.
5. **Respect the goals file's constraints.** Skip any recommendation that conflicts with them and say why.
6. **Read-only by default.** Only `write_goals`, `write_report` and approved code edits change files.

## Workflow

### 1. Goals (once per project)
Call `read_goals`. If it exists and is valid, summarise it in two lines and ask if it's still right.
If not, interview the user — **one round, at most six questions**, offering sensible options:
- What does the app do? (one sentence)
- What counts as a *successful task*? (e.g. ticket resolved without a human, answer accepted, PR merged)
- Roughly how many tasks per month?
- Is there a target cost per successful task, or a monthly budget?
- How much may the success rate drop for a cheaper setup? (default: 1 percentage point)
- Any constraints? (must stay on a provider, latency limits, data rules) — and where are the usage logs?

Confirm the answers back, then `write_goals`.

### 2. Scan
Call `scan`. Report: number of LLM call sites, how many have an eval, and the high-severity findings with
`file:line`. Use `explain_rule` if the user asks why something matters.

### 3. Measure (if usage logs exist)
Call `measure`. Lead with **cost per successful task** and its confidence interval, the success rate, the
cache hit rate, and the gap to the target. Mention spend that falls outside task outcomes (e.g. batch jobs).
No logs? Say the savings are structural estimates, and offer to add `harnessdoc.log.UsageLogger`
(two lines: `record(response, task_id=…)` after each LLM call, `outcome(task_id, success)` at the end).

### 4. Report
Call `write_report` and give the user the path plus the top three recommendations: what, where, measured
saving, guard eval (or "none — write one first"), and prerequisites (e.g. HD001/HD003 before HD002).

### 5. Fix, one at a time (only if the user asks)
For the chosen recommendation:
1. If it can change outputs (model swap, regex instead of LLM, prompt edits) and has no guard eval, write the
   guard eval first and run it on the current code to get a baseline.
2. Propose the smallest diff that implements the fix. Wait for approval.
3. Apply it, run the guard eval, and report pass/fail against the goals file's quality bar.
4. Re-run `scan` to confirm the finding is gone. When new logs exist, re-run `measure` and compare cost per
   successful task with the earlier baseline.

## Reading the findings

| Rule | Meaning | Usually do first |
|---|---|---|
| HD001 | Timestamp/UUID at the start of a prompt busts the cache | — |
| HD002 | Big static prefix sent to Anthropic without `cache_control` | HD001, HD003 |
| HD003 | Tool list built from a `set` (order varies per process) | — |
| HD004 | `while True` retry that never gives up | — |
| HD005 | Agent loop with no turn cap | — |
| HD006 | Premium model doing classification / extraction / short summaries | guard eval |
| HD007 | Huge `max_tokens` for a one-word answer | — |
| HD008 | LLM extracting something a regex can find | guard eval |
| HD009 | Offline job calling the real-time API in a loop (use Batch API) | — |
| HD010 | No eval references this call site | — |
