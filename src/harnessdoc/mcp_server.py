"""harnessdoc as an MCP server, so any agent (Claude Code, Cursor, Codex…) can get the numbers.

    harnessdoc mcp                      # stdio transport
    claude mcp add harnessdoc -- harnessdoc mcp

harnessdoc makes no LLM calls of its own. The connected agent does the reasoning and the conversation; these
tools return deterministic measurements. Read-only tools are annotated as such; only `write_goals`,
`write_report`, `scaffold_guard` and `run_experiment` create files, and only inside the project directory.
`run_experiment` is the one exception to "no LLM calls": it runs the *user's* code with the user's API keys,
so it requires an explicit budget and `user_approved=true`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import (
    ToolError,  # anticipated failures reach the agent as readable errors
)
from mcp.types import ToolAnnotations

from . import __version__
from .goals import Goals, GoalsError, goals_path, load_goals, save_goals
from .measure import combined_saving, measure_findings
from .pricing import PriceBook, UnknownModel, Usage
from .report import analyse, build_report
from .rules import explain
from .scan import scan_project
from .usage import load_logs, load_outcomes_file
from .usage.analyze import baseline

INSTRUCTIONS = """harnessdoc measures what each successful task of an AI app costs and where the harness wastes
tokens. Workflow: read_goals (or interview the user and write_goals) → scan → measure (if usage logs exist) →
write_report → for each approved change: scaffold_guard if the call has no eval, apply, then compare
before/after logs. Quote only numbers these tools return; savings are estimates until compare says SHIP.
Never change a call site's model or prompt while it has no guard eval (findings list `guard`)."""

server = MCPServer("harnessdoc", instructions=INSTRUCTIONS, version=__version__)
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _root(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    return p


def _inside(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"{rel} is outside the project directory")
    return target


def _goals(root: Path) -> Goals | None:
    try:
        return load_goals(root)
    except GoalsError as e:
        raise ToolError(f"harnessdoc.goals.yaml is invalid: {e}") from None


def _site_summary(s) -> dict[str, Any]:
    return {
        "id": s.id, "provider": s.provider, "api": s.api, "function": s.function,
        "model": s.model.value if s.model.kind == "literal" else None,
        "max_tokens": s.max_tokens, "static_prompt_tokens": s.system.tokens,
        "cached": s.has_cache_control, "batch_api": s.batch_api,
    }


# ----------------------------------------------------------------------------- tools


@server.tool(annotations=READ_ONLY)
def scan(project_path: str, min_severity: str = "info") -> dict[str, Any]:
    """Statically scan a Python project for LLM call sites, harness waste (rules HD001–HD010) and eval coverage.

    Never runs or imports the project. Returns call sites, findings (file:line, evidence, fix, estimated
    saving, guard eval) and which call sites have no eval.
    """
    order = {"info": 0, "low": 1, "medium": 2, "high": 3}
    if min_severity not in order:
        raise ToolError("min_severity must be info, low, medium or high")
    root = _root(project_path)
    r = scan_project(root, PriceBook.load(root))
    sites = [s for s in r.call_sites if not s.is_test_file]
    return {
        "files_scanned": r.files_scanned,
        "call_sites": [_site_summary(s) for s in sites],
        "uncovered_call_sites": [s.id for s in sites if not r.coverage.get(s.id)],
        "findings": [f.as_dict() for f in r.findings if order[f.severity] >= order[min_severity]],
        "parse_errors": r.errors,
    }


@server.tool(annotations=READ_ONLY)
def measure(project_path: str, logs: list[str] | None = None, outcomes: str | None = None,
            task_key: str | None = None) -> dict[str, Any]:
    """Measure spend, cache hit rate, success rate and cost per successful task from usage logs, and price
    each scan finding with its call site's measured spend.

    `logs` are paths relative to the project (default: the goals file's `logs`). Formats are auto-detected:
    raw Anthropic/OpenAI/Gemini responses as JSONL, OpenTelemetry GenAI spans, harnessdoc UsageLogger files.
    """
    root = _root(project_path)
    goals = _goals(root)
    paths = [_inside(root, p) for p in (logs or (goals.logs if goals else []))]
    if not paths:
        raise ToolError("no logs given and none listed in harnessdoc.goals.yaml")
    book = PriceBook.load(root)
    key = task_key or (goals.task_key if goals else None)
    loaded = load_logs([p for p in paths if p.exists()], key)
    out_path = outcomes or (goals.outcomes if goals else None)
    extra = load_outcomes_file(_inside(root, out_path), key or "task_id") if out_path else []
    base = baseline(loaded.records, loaded.outcomes + extra, book)
    measured = measure_findings(scan_project(root, book), base, book)
    window, month = combined_saving(measured)
    return {
        "missing_log_paths": [str(p.relative_to(root)) for p in paths if not p.exists()],
        "unreadable_lines": loaded.error_count,
        "baseline": base.as_dict(),
        "findings": [
            {"rule": m.finding.rule, "where": f"{m.finding.file}:{m.finding.line}", "title": m.finding.title,
             "saving": m.saving, "saving_month": m.saving_month, "basis": m.basis, "guard": m.finding.guard}
            for m in measured
        ],
        "combined_saving": {"window": window, "month": month},
    }


@server.tool(annotations=READ_ONLY)
def price(model: str, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
          cache_write_tokens: int = 0, cache_ttl: str | None = None, batch: bool = False) -> dict[str, Any]:
    """Price token counts for a model from harnessdoc's price book (USD). Token counts must be disjoint:
    `input_tokens` excludes cached reads and writes."""
    book = PriceBook.load()
    try:
        c = book.cost(model, Usage(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                                   cache_ttl, batch))
    except UnknownModel:
        raise ToolError(f"unknown model {model!r}; known: {', '.join(book.models)}") from None
    return c.as_dict() | {"price_book_as_of": book.as_of}


@server.tool(annotations=READ_ONLY)
def explain_rule(rule: str) -> dict[str, str]:
    """Explain a harnessdoc rule (HD001–HD010): why it costs money, how to fix it, how the saving is estimated."""
    try:
        return explain(rule)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from None


@server.tool(annotations=READ_ONLY)
def read_goals(project_path: str) -> dict[str, Any]:
    """Read harnessdoc.goals.yaml (what the app does, what counts as success, target, constraints, logs).
    Returns {"exists": false} if the project has none yet — then interview the user and call write_goals."""
    root = _root(project_path)
    try:
        g = load_goals(root)
    except GoalsError as e:
        return {"exists": True, "valid": False, "error": str(e)}
    return {"exists": False} if g is None else {"exists": True, "valid": True, "goals": g.as_dict()}


@server.tool(annotations=WRITES)
def write_goals(project_path: str, app: str, success: str, monthly_tasks: int | None = None,
                target_cost_per_success: float | None = None, quality_bar: str = "",
                max_success_rate_drop: float = 1.0, priorities: list[str] | None = None,
                constraints: list[str] | None = None, logs: list[str] | None = None,
                outcomes: str | None = None, task_key: str | None = None, notes: str = "") -> dict[str, Any]:
    """Create or replace harnessdoc.goals.yaml in the project with the user's answers. Only call this after
    the user has confirmed the answers."""
    root = _root(project_path)
    for p in (logs or []) + ([outcomes] if outcomes else []):
        _inside(root, p)
    g = Goals(app=app, success=success, monthly_tasks=monthly_tasks,
              target_cost_per_success=target_cost_per_success, quality_bar=quality_bar,
              max_success_rate_drop=max_success_rate_drop, priorities=priorities or ["cost", "quality"],
              constraints=constraints or [], logs=logs or [], outcomes=outcomes, task_key=task_key, notes=notes)
    try:
        path = save_goals(g, root)
    except GoalsError as e:
        raise ToolError(f"goals not saved: {e}") from None
    return {"written": str(path.relative_to(root)), "goals": g.as_dict()}


@server.tool(annotations=WRITES)
def write_report(project_path: str, out: str = "harnessdoc-report.md", logs: list[str] | None = None) -> dict:
    """Write the Markdown report (goal vs baseline, ranked recommendations, eval coverage, method) into the
    project and return its path and headline numbers."""
    root = _root(project_path)
    target = _inside(root, out)
    a = analyse(root, [_inside(root, p) for p in logs] if logs else None, goals=_goals(root))
    target.write_text(build_report(a), encoding="utf-8")
    headline: dict[str, Any] = {"findings": len(a.scan.findings)}
    if a.base and a.base.cost_per_success:
        headline["cost_per_success"] = a.base.cost_per_success.value
    if a.measured:
        headline["combined_saving_month"] = combined_saving(a.measured)[1]
    return {"written": str(target.relative_to(root)), **headline,
            "goals_file": str(goals_path(root).relative_to(root)) if goals_path(root).exists() else None}


@server.tool(annotations=READ_ONLY)
def compare(project_path: str, before: list[str], after: list[str], max_success_rate_drop: float | None = None,
            site: str | None = None) -> dict[str, Any]:
    """Before/after verdict for a change: SHIP, REJECT, INCONCLUSIVE or NO CLEAR SAVING.

    Compares cost per successful task (95% CI of the change) and tests quality for non-inferiority against
    the allowed success-rate drop (points; default from the goals file, else 1). `before`/`after` are log
    paths relative to the project. When INCONCLUSIVE, `tasks_needed_per_window` says how much data would
    settle it — report that instead of declaring a winner.
    """
    from .compare import compare as run_compare

    root = _root(project_path)
    goals = _goals(root)
    key = goals.task_key if goals else None
    b = load_logs([_inside(root, p) for p in before], key)
    a = load_logs([_inside(root, p) for p in after], key)
    if not b.records or not a.records:
        raise ToolError("no LLM usage found in the before or after logs")
    drop = max_success_rate_drop if max_success_rate_drop is not None else (
        goals.max_success_rate_drop if goals else 1.0)
    c = run_compare((b.records, b.outcomes), (a.records, a.outcomes), PriceBook.load(root), drop, site)
    return c.as_dict()


@server.tool(annotations=READ_ONLY)
def check(project_path: str, baseline: str = "harnessdoc-baseline.json", fail_on: str = "medium") -> dict:
    """CI-style gate: which findings are new compared with the committed baseline, which were fixed, and
    whether the gate would fail (new findings at or above `fail_on`)."""
    from .check import check as run_check
    from .check import read_baseline

    if fail_on not in ("high", "medium", "low", "info", "none"):
        raise ToolError("fail_on must be high, medium, low, info or none")
    root = _root(project_path)
    path = _inside(root, baseline)
    known = None
    if path.exists():
        try:
            known = read_baseline(path)
        except (ValueError, KeyError) as e:
            raise ToolError(f"can't read baseline: {e}") from None
    res = run_check(scan_project(root, PriceBook.load(root)), known)
    return res.as_dict(fail_on) | {"baseline_found": known is not None, "would_fail": bool(res.failing(fail_on))}


@server.tool(annotations=WRITES)
def scaffold_guard(project_path: str, site: str, tests_dir: str = "tests/evals") -> dict[str, Any]:
    """Create a guard eval (pytest file + empty JSONL cases file) for an LLM call site that has none.
    Never overwrites. Fill the cases file with 10–30 real inputs and expected outputs before changing the
    call; until it has a case, the call site still counts as uncovered."""
    from .guard import GuardError, scaffold

    root = _root(project_path)
    try:
        s = scaffold(root, site, tests_dir)
    except GuardError as e:
        raise ToolError(str(e)) from None
    return {"call_site": s.site.id, "test_file": str(s.test_file.relative_to(root)),
            "cases_file": str(s.cases_file.relative_to(root)),
            "created": [str(p.relative_to(root)) for p in s.created]}


SPENDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def _spec(root: Path, spec: str):
    from .experiment import ExperimentError, Spec

    try:
        return Spec.load(_inside(root, spec))
    except ExperimentError as e:
        raise ToolError(str(e)) from None


@server.tool(annotations=READ_ONLY)
def plan_experiment(project_path: str, spec: str = "harnessdoc.experiment.yaml") -> dict[str, Any]:
    """Describe an experiment before running it: cases, variants, number of runs and — when usage logs for the
    call site exist — an estimated cost and suggested budget. Calls nothing. Show this to the user and get
    their approval and a budget before calling run_experiment."""
    from .experiment import ExperimentError, site_avg_cost
    from .experiment import plan_experiment as plan

    root = _root(project_path)
    s = _spec(root, spec)
    book = PriceBook.load(root)
    avg, model = site_avg_cost(root, s.function_name, book)
    try:
        p = plan(s, root, book, avg, model)
    except ExperimentError as e:
        raise ToolError(str(e)) from None
    return {"name": s.name, "call": s.call, "cases": p.cases, "runs": p.runs, "variants": p.variants,
            "baseline": p.variants[0], "estimated_cost_usd": p.est_cost_usd,
            "suggested_budget_usd": p.suggested_budget_usd, "basis": p.basis}


@server.tool(annotations=SPENDS)
def run_experiment(project_path: str, budget_usd: float, user_approved: bool,
                   spec: str = "harnessdoc.experiment.yaml", max_cases: int | None = None) -> dict[str, Any]:
    """Run an experiment: call the project's function on every case under each variant, with the user's API
    keys from the environment. COSTS MONEY. Only call after showing plan_experiment to the user and getting
    explicit approval and a budget; pass user_approved=true. Stops before exceeding budget_usd. Returns a
    verdict per variant (SHIP / REJECT / INCONCLUSIVE / NO CLEAR SAVING) and writes the full results file."""
    import json
    import re

    from .experiment import ExperimentError
    from .experiment import run_experiment as run

    if not user_approved:
        raise ToolError("not run: experiments spend money with the user's API keys. Show plan_experiment to the "
                        "user, ask for approval and a budget, then call again with user_approved=true.")
    if budget_usd <= 0 or budget_usd > 100:
        raise ToolError("budget_usd must be between 0 and 100; larger runs should go through the CLI")
    root = _root(project_path)
    s = _spec(root, spec)
    try:
        r = run(s, root, budget_usd, PriceBook.load(root), max_cases)
    except ExperimentError as e:
        raise ToolError(str(e)) from None
    out = root / f"harnessdoc-experiment-{re.sub(r'[^A-Za-z0-9_.-]', '-', s.name)}.json"
    out.write_text(json.dumps(r.as_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return {
        "spent_usd": r.spent_usd, "budget_usd": r.budget_usd, "stopped_early": r.stopped_early,
        "sdk_targets_patched": r.sdk_targets_patched, "unpriced_models": r.unpriced_models,
        "variants": [{"name": v.name, "runs": v.runs, "passed": v.passed, "cost_usd": v.cost, "errors": v.errors,
                      "verdict": v.verdict["verdict"] if v.verdict else "baseline",
                      "reasons": v.verdict["reasons"] if v.verdict else []} for v in r.variants],
        "results_file": str(out.relative_to(root)),
    }
