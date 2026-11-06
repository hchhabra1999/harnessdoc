"""Deterministic checks over LLM call sites. Each check returns Findings with file:line evidence.

Savings are *estimates* derived from the price book and the call's structure. They become measured numbers
only when usage logs are joined (harnessdoc measure) or a before/after run proves them.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..pricing import PriceBook, UnknownModel
from .model import CallSite, Finding

PREMIUM_INPUT_PRICE = 4.0  # USD / 1M input tokens and above = "premium tier" for this check

SMALL_MODEL = {  # cheapest current general-purpose model per provider (a starting point to try, not a verdict)
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-6-luna",
    "gemini": "gemini-3.1-flash-lite",
}

SIMPLE_TASK = [
    (re.compile(r"\bclassify\b|\bexactly one of\b|\bone of these\b|\byes or no\b|\bcategori[sz]e\b"),
     "classification"),
    (re.compile(r"\bsummari[sz]e\b.*\b(one|two|three|1|2|3) sentences?\b|\btl;?dr\b"), "short summary"),
    (re.compile(r"\bextract\b"), "extraction"),
]
SHORT_ANSWER = re.compile(r"\breply with (the |only |just )?\w+( \w+)? only\b|\bone word\b|\byes or no\b|"
                          r"\bexactly one of\b|\breply with only\b|\bonly the\b")
REGEXABLE = [
    (re.compile(r"\b[a-z]{2,}-\d{3,}\b"), "an ID with a fixed format"),
    (re.compile(r"\bemail address(es)?\b"), "email addresses"),
    (re.compile(r"\bphone numbers?\b"), "phone numbers"),
    (re.compile(r"\burls?\b|\blinks?\b"), "URLs"),
    (re.compile(r"\bdates?\b \(?format"), "dates in a known format"),
]
BATCH_HINTS = re.compile(r"\b(nightly|batch|cron|jobs?|etl|backfill|offline|scheduled|daily|weekly|"
                         r"nobody waits|report)\b")


def _where(site: CallSite, kw: str) -> int:
    return site.kw_lines.get(kw, site.line)


def _model_id(site: CallSite) -> str | None:
    return site.model.value if site.model.kind == "literal" and isinstance(site.model.value, str) else None


# ----------------------------------------------------------------------------- caching


def check_cache_busting_prefix(site: CallSite, book: PriceBook) -> list[Finding]:
    s = site.system
    if not s.volatile:
        return []
    file = s.source_file or site.file
    line = s.volatile_line or site.line
    what = ", ".join(sorted(set(s.volatile)))
    if s.leading_volatile:
        return [Finding(
            rule="HD001", title="Volatile value at the start of the prompt busts the cache",
            category="caching", severity="high", file=file, line=line, call_site=site.id,
            evidence=f"`{what}` is rendered before ~{s.tokens or '?'} tokens of static prompt "
                     f"(system built by `{s.value}`); the prefix changes on every request.",
            recommendation="Move volatile values to the end of the prompt (after the last cache breakpoint) or "
                           "into the user turn, so the static prefix is byte-identical across requests.",
            est_saving="most of the static prompt's input cost per request once cacheable (measure with logs)",
            related_lines=[site.line],
        )]
    return [Finding(
        rule="HD001", title="Volatile value inside the system prompt",
        category="caching", severity="medium", file=file, line=line, call_site=site.id,
        evidence=f"`{what}` is rendered inside the system prompt; anything after it cannot be cached.",
        recommendation="Keep the system prompt static; pass time/IDs in the user turn.",
        related_lines=[site.line],
    )]


def check_missing_cache_control(site: CallSite, book: PriceBook) -> list[Finding]:
    if site.provider != "anthropic" or site.has_cache_control or site.batch_api:
        return []
    prefix = site.system.tokens or 0
    has_tools = site.tools.kind != "absent"
    mid = _model_id(site)
    try:
        price = book.price(mid) if mid else None
    except UnknownModel:
        price = None
    minimum = (price.min_cache_tokens if price else None) or 1024
    if prefix < minimum and not (has_tools and prefix >= minimum // 2):
        return []
    pct = None
    saving = "cached reads cost ~0.1x of input"
    if price and price.cache_read is not None:
        pct = round(1 - price.cache_read / price.input, 3)
        saving = (f"~{pct:.0%} off the ~{prefix} static prefix tokens on every request after the first "
                  f"(cache read ${price.cache_read:g} vs input ${price.input:g} per 1M)")
    return [Finding(
        rule="HD002", title="Large static prefix sent without prompt caching",
        category="caching", severity="high", file=site.file, line=site.line, call_site=site.id,
        evidence=f"~{prefix} tokens of system prompt{' + tool definitions' if has_tools else ''} are re-sent "
                 f"uncached on every request (no `cache_control`); minimum cacheable prefix for "
                 f"{mid or 'this model'} is {minimum} tokens.",
        recommendation="Add a `cache_control` breakpoint on the last system block (and top-level automatic "
                       "caching for multi-turn tails). Fix any cache-busting prefix (HD001/HD003) first.",
        est_saving=saving, est_saving_pct=pct,
    )]


def check_nondeterministic_tools(site: CallSite, book: PriceBook) -> list[Finding]:
    t = site.tools
    if not t.nondeterministic_line:
        return []
    return [Finding(
        rule="HD003", title="Tool list order is not deterministic",
        category="caching", severity="high", file=t.source_file or site.file, line=t.nondeterministic_line,
        call_site=site.id,
        evidence=f"Tools passed at {site.id} are built by iterating a set; set order differs between "
                 "processes, and tools are the first bytes of the request, so every worker has its own "
                 "cache prefix.",
        recommendation="Build tools from a list or `sorted(...)` so the serialized tool list is identical "
                       "in every process.",
        related_lines=[site.line],
    )]


# ----------------------------------------------------------------------------- loops


def check_unbounded_retry(site: CallSite, book: PriceBook) -> list[Finding]:
    if not (site.retry_continue and site.in_while_true and not site.loop_has_counter):
        return []
    return [Finding(
        rule="HD004", title="Unbounded manual retry around an LLM call",
        category="loop", severity="high", file=site.file, line=site.retry_continue, call_site=site.id,
        evidence=f"`while True` (line {site.in_while_true}) + an exception handler that `continue`s "
                 f"(line {site.retry_continue}) retries forever — including non-retryable 4xx errors.",
        recommendation="Remove the manual retry and rely on the SDK's built-in retries (429/5xx, with "
                       "backoff); let 4xx errors fail fast. If you must retry, cap attempts and back off.",
        related_lines=[site.in_while_true],
    )]


def check_uncapped_agent_loop(site: CallSite, book: PriceBook) -> list[Finding]:
    if not site.in_while_true or site.loop_has_counter:
        return []
    return [Finding(
        rule="HD005", title="Agent loop has no iteration cap",
        category="loop", severity="medium", file=site.file, line=site.in_while_true, call_site=site.id,
        evidence=f"The LLM call at line {site.line} runs inside `while True` with no turn counter; a confused "
                 "agent can keep calling tools until the context window fills.",
        recommendation="Cap tool turns (e.g. `for turn in range(MAX_TURNS)`) and escalate or answer when "
                       "the cap is hit.",
        related_lines=[site.line],
    )]


# ----------------------------------------------------------------------------- model / output


def check_premium_model_simple_task(site: CallSite, book: PriceBook) -> list[Finding]:
    mid = _model_id(site)
    if not mid:
        return []
    try:
        price = book.price(mid)
    except UnknownModel:
        return []
    if price.input < PREMIUM_INPUT_PRICE:
        return []
    task = next((label for rx, label in SIMPLE_TASK if rx.search(site.prompt_text)), None)
    if not task:
        return []
    small_id = SMALL_MODEL.get(price.provider)
    pct, saving = None, None
    if small_id:
        small = book.price(small_id)
        pct = round(1 - (small.input + small.output) / (price.input + price.output), 3)
        saving = (f"~{pct:.0%} of this call site's cost if {small_id} holds quality "
                  f"(${small.input:g}/${small.output:g} vs ${price.input:g}/${price.output:g} per 1M)")
    return [Finding(
        rule="HD006", title=f"Premium model used for a {task} task",
        category="model", severity="medium", file=site.file, line=_where(site, "model"), call_site=site.id,
        evidence=f"`{mid}` (${price.input:g} in / ${price.output:g} out per 1M) handles what the prompt "
                 f"describes as {task}.",
        recommendation=f"Try a small model{f' (`{small_id}`)' if small_id else ''} or lower effort, and keep "
                       "the change only if the eval for this call site holds. Write that eval first if none exists.",
        est_saving=saving, est_saving_pct=pct,
    )]


def check_oversized_max_tokens(site: CallSite, book: PriceBook) -> list[Finding]:
    if site.max_tokens is None or site.max_tokens < 1024 or not SHORT_ANSWER.search(site.prompt_text):
        return []
    return [Finding(
        rule="HD007", title="Output cap far larger than the expected answer",
        category="output", severity="low", file=site.file, line=_where(site, "max_tokens"), call_site=site.id,
        evidence=f"`max_tokens={site.max_tokens}` for a prompt that asks for a short answer.",
        recommendation="Set a tight cap (e.g. 16–64) or use structured output; it bounds runaway generations "
                       "and makes truncation visible.",
        est_saving="none unless the model over-generates; it's a ceiling, not a price",
    )]


def check_regexable_extraction(site: CallSite, book: PriceBook) -> list[Finding]:
    if "extract" not in site.prompt_text:
        return []
    what = next((label for rx, label in REGEXABLE if rx.search(site.prompt_text)), None)
    if not what:
        return []
    return [Finding(
        rule="HD008", title="LLM used for extraction a regex can do",
        category="code", severity="medium", file=site.file, line=site.line, call_site=site.id,
        evidence=f"The prompt asks the model to extract {what}.",
        recommendation="Use a regular expression; fall back to the LLM only when it finds nothing (or never).",
        est_saving="~100% of this call site's cost on inputs the regex handles", est_saving_pct=1.0,
    )]


def check_batchable_loop(site: CallSite, book: PriceBook) -> list[Finding]:
    if not site.in_for_loop or site.batch_api or site.streaming or site.is_test_file:
        return []
    # split snake_case / kebab-case / paths into words so `nightly_backfill` and `jobs/x.py` both match
    if not BATCH_HINTS.search(re.sub(r"[_\-./]", " ", site.context_text)):
        return []
    mid = _model_id(site)
    multiplier = 0.5
    if mid:
        try:
            multiplier = book.price(mid).batch_multiplier
        except UnknownModel:
            pass
    if multiplier >= 1.0:
        return []
    pct = round(1 - multiplier, 3)
    return [Finding(
        rule="HD009", title="Offline loop uses the real-time API",
        category="batch", severity="medium", file=site.file, line=site.line, call_site=site.id,
        evidence=f"One real-time request per item inside a loop (line {site.in_for_loop}) in code that looks "
                 f"non-interactive ({site.file}, `{site.function}`).",
        recommendation="Submit the items through the provider's Batch API and collect results asynchronously.",
        est_saving=f"~{pct:.0%} of this job's cost", est_saving_pct=pct,
        related_lines=[site.in_for_loop],
    )]


CHECKS: list[Callable[[CallSite, PriceBook], list[Finding]]] = [
    check_cache_busting_prefix,
    check_missing_cache_control,
    check_nondeterministic_tools,
    check_unbounded_retry,
    check_uncapped_agent_loop,
    check_premium_model_simple_task,
    check_oversized_max_tokens,
    check_regexable_extraction,
    check_batchable_loop,
]


def coverage_findings(sites: list[CallSite], coverage: dict[str, list[str]]) -> list[Finding]:
    out = []
    for s in sites:
        if s.is_test_file or coverage.get(s.id):
            continue
        out.append(Finding(
            rule="HD010", title="No eval covers this LLM call",
            category="coverage", severity="info", file=s.file, line=s.line, call_site=s.id,
            evidence=f"No test/eval file references `{s.function or s.id}`.",
            recommendation="Add a guard eval (a handful of real inputs with expected outputs) before changing "
                           "this call's model, prompt or caching, so savings can be proven safe.",
        ))
    return out
