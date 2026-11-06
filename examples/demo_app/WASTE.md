# Answer key — planted harness waste in the Acme demo app

This app is **deliberately wasteful**. It is harnessdoc's benchmark: every scanner release is scored on how
many of these it finds (recall) and how many findings are *not* on this list (false positives).
Do not "fix" the app — fixes go in a copy during before/after demos.

| ID | Location | Category | What's wasteful | Expected recommendation | Guard eval today? |
|---|---|---|---|---|---|
| W01 | `acme_support/agent.py:15` | caching | Timestamp at the **start** of the system prompt changes the prefix on every request, so nothing after it can ever be cached | Move volatile values after the last cache breakpoint (or into the user turn) | No |
| W02 | `acme_support/agent.py:26` | caching | ~1.4K-token static prefix (tools + policy) re-sent uncached on every turn of every conversation; no `cache_control` | Add a breakpoint on the last system block (+ automatic caching for the conversation tail) | No |
| W03 | `acme_support/tools.py:36` | caching | Tool list built by iterating a `set` → order differs between processes → tools (first in render order) break the cache across workers | Build tools from a list / sort by name | No |
| W04 | `acme_support/agent.py:24,34` | loop hygiene | `while True` + `except APIError: continue` retries forever, including non-retryable 4xx; the SDK already retries 429/5xx | Remove the manual retry; let the SDK retry; fail fast on 4xx | No |
| W05 | `acme_support/agent.py:24` | loop hygiene | Tool loop has no turn/iteration cap — a confused agent can loop until the context fills | Cap tool turns (policy says escalate after 3 tool calls) | No |
| W06 | `acme_support/router.py:12` | model choice | Top-tier model (`claude-opus-5-5`, $4/$20) for a 6-label classification | Try a small model (e.g. `claude-haiku-5-5`) / low effort; keep only if the eval holds | **Yes** — `tests/evals/test_router.py` |
| W07 | `acme_support/router.py:13` | output hygiene | `max_tokens=16000` for a one-word answer (no cost unless the model rambles, but no ceiling either) | Set a tight cap (~16–64) or use structured output | Yes |
| W08 | `acme_support/extract.py:9` | replace with code | LLM call to find a string matching `ORD-\d{6}` | Regex; call the LLM only if the regex finds nothing (or never) | No |
| W09 | `jobs/nightly_summary.py:10-11` | batch | Non-interactive nightly job calls the real-time API once per ticket in a loop | Batch API (~50% off) | No |
| W10 | `jobs/nightly_summary.py:12` | model choice | Premium model (`gpt-6-astra`, $10/$50) for two-sentence dashboard summaries | Cheaper tier — but **write a guard eval first** | No |
| W11 | repo-wide | eval coverage | 4 LLM call sites, 1 covered by an eval (router). Agent, extraction and summaries have no quality guard, so no cost change to them can be proven safe | Generate guard evals for uncovered call sites before changing them | — |

**Not waste (should NOT be flagged):** the policy prompt length itself (it is needed and cacheable), the
`stop_reason` check in the agent loop, and the router's validation of the returned label.
