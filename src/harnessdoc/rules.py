"""What each check means, why it costs money, how to fix it and how the saving is estimated."""

from __future__ import annotations

RULES: dict[str, dict[str, str]] = {
    "HD001": {
        "title": "Volatile value at the start of the prompt",
        "why": "Prompt caching matches byte-identical prefixes. A timestamp, UUID or random value rendered "
               "before static text makes every request's prefix unique, so nothing after it is ever cached.",
        "fix": "Move volatile values after the last cache breakpoint or into the user turn.",
        "saving": "Enables HD002 on the same call; priced there.",
    },
    "HD002": {
        "title": "Large static prefix sent without prompt caching (Anthropic)",
        "why": "Anthropic caching is opt-in. Without `cache_control`, a long system prompt and tool list are "
               "billed at the full input rate on every request; cached reads cost a fraction of that.",
        "fix": "Add a `cache_control` breakpoint on the last system block, plus top-level automatic caching "
               "for multi-turn conversations. Fix HD001/HD003 first or the cache never hits.",
        "saving": "static prefix tokens × requests × (input price − cache-read price), capped at the call's "
                  "measured uncached input spend; excludes occasional cache writes.",
    },
    "HD003": {
        "title": "Tool list order is not deterministic",
        "why": "Tools render first in the request. Building them by iterating a set gives a different order "
               "per process (hash randomisation), so each worker has its own cache prefix.",
        "fix": "Build tools from a list or sort them by name.",
        "saving": "Enables HD002 on the same call; priced there.",
    },
    "HD004": {
        "title": "Unbounded manual retry",
        "why": "`while True` plus an exception handler that `continue`s retries forever — including on "
               "non-retryable 4xx errors — and every retry is billed input.",
        "fix": "Use the SDK's built-in retries (429/5xx with backoff) and let 4xx fail fast; if you must retry, "
               "cap attempts.",
        "saving": "Not priced: depends on error rates, visible only in logs with error records.",
    },
    "HD005": {
        "title": "Agent loop without an iteration cap",
        "why": "A confused agent can keep calling tools, re-sending a growing history each turn, until the "
               "context window fills.",
        "fix": "Cap turns (e.g. `for turn in range(MAX_TURNS)`) and escalate or answer at the cap.",
        "saving": "Not priced: it bounds the worst case rather than the average.",
    },
    "HD006": {
        "title": "Premium model for a simple task",
        "why": "Classification, extraction and short summaries rarely need the most expensive tier.",
        "fix": "Try the provider's small model (or lower effort) and keep it only if the call's eval holds. "
               "Write that eval first if none exists.",
        "saving": "1 − (small model input+output price) / (premium input+output price) of the call's spend — "
                  "an upper bound that assumes the same token counts.",
    },
    "HD007": {
        "title": "Output cap far larger than the expected answer",
        "why": "`max_tokens` in the thousands for a one-word answer doesn't cost anything by itself, but it "
               "removes the ceiling on runaway generations and hides truncation bugs.",
        "fix": "Set a tight cap or use structured output.",
        "saving": "None directly.",
    },
    "HD008": {
        "title": "LLM used for regex-able extraction",
        "why": "IDs with a fixed format, emails and URLs are found exactly and for free by a regular expression.",
        "fix": "Use a regex; call the model only when the regex finds nothing (or never).",
        "saving": "Up to 100% of the call's spend on inputs the regex handles.",
    },
    "HD009": {
        "title": "Offline loop on the real-time API",
        "why": "Jobs nobody waits on (nightly, backfills, reports) can use the provider's Batch API at about "
               "half price.",
        "fix": "Submit items as a batch and collect results asynchronously.",
        "saving": "(1 − batch multiplier) of the call's spend, from the price book (usually 50%).",
    },
    "HD010": {
        "title": "No eval covers this LLM call",
        "why": "Without an eval, no change to the call's model, prompt or caching can be shown to keep quality.",
        "fix": "Add a small guard eval (real inputs with expected outputs) before changing the call.",
        "saving": "None directly; it makes the other savings safe to take.",
    },
}


def explain(rule: str) -> dict[str, str]:
    key = rule.strip().upper()
    if key not in RULES:
        raise KeyError(f"unknown rule {rule!r}; known: {', '.join(sorted(RULES))}")
    return {"rule": key, **RULES[key]}
