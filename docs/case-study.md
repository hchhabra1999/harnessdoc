# Case study: running harnessdoc on two real open-source AI apps

**Date:** 2026-11-06 · **harnessdoc:** static scanner only (no usage logs, nothing executed) ·
**Apps:** [gptme](https://github.com/gptme/gptme) (MIT, terminal agent, 1,117 Python files) and
[optillm](https://github.com/algorithmicsuperintelligence/optillm) (Apache-2.0, LLM inference proxy, 148 files).
Shallow clones, parsed only — the scanner never imports or runs the code it reads. No issues or pull requests
were opened on either project.

The question was not "how many savings can we claim" but **"does the scanner hold up on code we didn't write?"**
Our demo app scored 10/10 — but we wrote it. This is the honest test, and it found three real bugs.

## What was scanned

| | gptme | optillm |
|---|---|---|
| Python files | 1,117 | 148 |
| Scan time (after fixes) | 11.7 s | 1.0 s |
| LLM call sites found (non-test) | 8 | 128 |
| Call-shaped lines found by a plain `grep`¹ | 8 | 126 |
| Call sites the scanner missed vs `grep` | **0** | **0** |
| Call sites with the model known statically | 1 of 8 | 3 of 128 |
| Findings | 3 × HD010 | 55 × HD010 |

¹ `grep` for `.messages.create(`, `.chat.completions.create(`, `.responses.create(`, `generate_content(` and
`litellm.completion(` outside tests. It isn't independent ground truth — it uses the same call shapes — so this
measures recall *of those shapes*, not of every way an app could call a model. optillm's two extra sites are
LiteLLM wrapper calls the grep pattern can't see.

A third candidate, `whatsapp-agentkit`, was dropped: it's a template whose coding agent writes the app, with one
Python file and no LLM calls to analyse.

## Three scanner bugs this found (all fixed, with regression tests)

1. **Too slow to use on a real repo.** The first gptme scan ran past 5 minutes. Eval coverage ran one regex per
   project function per test file. It now builds each test file's identifier set once: **>300 s → 11.7 s**.
2. **Eval coverage was wildly over-reported.** Matching by function name meant a test mentioning `chat` "covered"
   every `chat` in the repo: each gptme call site showed **298–713** covering test files, so `HD010` ("no eval
   covers this call") could never fire. Reachability now follows only names defined once in the project, and an
   ambiguous name counts only if the test also mentions the module. Counts dropped to **0–21**.
3. **Recall was ~30% on optillm.** 37 of 126 call sites were found. The scanner only trusted
   `client.chat.completions.create(...)` when the module imported an LLM SDK, but optillm passes `client` in as a
   function parameter and imports nothing. Call shapes unique to LLM APIs now count when the receiver looks like
   a client; the generic `messages.create` still needs the SDK import or both `model` and `max_tokens` (so a
   Twilio `messages.create` isn't mistaken for an LLM). **37 → 128 sites, and every grep-visible call is found.**

## What the scanner found — and didn't

- **Only `HD010` (info: no eval covers this call) fired.** Neither app triggered a cost rule. That is not "these
  apps are efficient": in both, the model is configured at runtime (a request field, a parameter, config), so only
  **4 of 136** call sites have a model the scanner can read, and every model-dependent check (HD006 premium model for
  a simple task, HD002 uncached prefix on a known model) is blind there. This is the scanner's biggest limitation on
  real apps, and the case for pairing it with usage logs, where the model is recorded per request.
- **`HD010` precision, checked by hand on gptme (3 findings):**
  - `llm_anthropic.py:1209` (`_stream_with_client`) and `llm_openai.py:2028` (`_stream_create`) look like **false
    positives**: they're reached only through `stream`-style functions whose names are ambiguous, so the scanner
    declines to follow them. (Not proven by running the tests.)
  - `scripts/github_ci_self_heal.py:151` (`_call_claude`) looks **right**: no test visibly calls the functions
    that use it.
- **`HD010` on optillm (55 findings):** 22 are in one-off benchmark and dataset scripts under `scripts/`, which are
  not product code. The other 33 were not hand-verified. Treat the count as an upper bound.
- **"Covered" means a test reaches the code, not that a test checks the model's output.** A test that mocks the LLM
  still counts. A guard eval proper (`harnessdoc guard scaffold`) is the stronger signal.

## What this does and doesn't show

- It shows the scanner finds LLM calls reliably on two unfamiliar codebases of very different shapes, in seconds.
- It does **not** show savings. Without usage logs nothing was priced, and no cost was measured. The roadmap target
  of ≥25% lower cost per successful task on a real app is still **unmet**: it needs an app with real traffic logs.
- Next improvements this points to: resolve models from function defaults and config, treat `scripts/` and
  benchmarks as non-product code for `HD010`, and follow calls through ambiguous names when the receiver's type is
  known.
