"""`harnessdoc` command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import __version__
from .pricing import PriceBook, UnknownModel, Usage

app = typer.Typer(
    help="harnessdoc — measure what each successful task of your AI app costs, and prove the savings.",
    no_args_is_help=True,
    add_completion=False,
)

PricingOpt = typer.Option(None, "--pricing", help="Override file or project dir with harnessdoc.pricing.json")


@app.command()
def version():
    """Print the harnessdoc version."""
    typer.echo(__version__)


@app.command()
def models(
    provider: str | None = typer.Option(None, help="anthropic | openai | gemini"),
    pricing: Path | None = PricingOpt,
):
    """List models in the price book with input/output prices (USD per 1M tokens)."""
    book = PriceBook.load(pricing)
    typer.echo(f"Price book as of {book.as_of}")
    for mid in book.models:
        p = book.price(mid)
        if provider and p.provider != provider:
            continue
        read = "—" if p.cache_read is None else f"{p.cache_read:g}"
        typer.echo(f"  {p.provider:<10} {mid:<24} in {p.input:>7g}  out {p.output:>7g}  cache-read {read}")


@app.command()
def cost(
    model: str = typer.Argument(..., help="Model id as written in your code"),
    input_tokens: int = typer.Option(0, "--in", help="Uncached input tokens"),
    output_tokens: int = typer.Option(0, "--out", help="Output tokens"),
    cache_read: int = typer.Option(0, "--cache-read", help="Cached input tokens read"),
    cache_write: int = typer.Option(0, "--cache-write", help="Input tokens written to cache"),
    ttl: str | None = typer.Option(None, help="Cache TTL for writes (e.g. 5m, 1h, 30m, 12h)"),
    batch: bool = typer.Option(False, help="Priced through the batch API"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    pricing: Path | None = PricingOpt,
):
    """Price a request (or a total) for a model."""
    book = PriceBook.load(pricing)
    usage = Usage(input_tokens, output_tokens, cache_read, cache_write, ttl, batch)
    try:
        c = book.cost(model, usage)
    except UnknownModel:
        typer.echo(f"Unknown model '{model}'. Run `harnessdoc models` or add it to harnessdoc.pricing.json.",
                   err=True)
        raise typer.Exit(2) from None
    if as_json:
        typer.echo(json.dumps(c.as_dict(), indent=2))
        return
    typer.echo(f"{c.model}{' (batch)' if c.batch else ''}{' (long context)' if c.long_context else ''}")
    for label, v in (("uncached input", c.uncached_input), ("cache read", c.cache_read),
                     ("cache write", c.cache_write), ("output", c.output)):
        if v:
            typer.echo(f"  {label:<15} ${v:,.6f}")
    typer.echo(f"  {'total':<15} ${c.total:,.6f}")


SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}


@app.command()
def scan(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Project root to scan"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output (stable ordering)"),
    fail_on: str = typer.Option("none", help="Exit 1 if any finding is at or above: high|medium|low|info|none"),
    pricing: Path | None = PricingOpt,
):
    """Statically scan a Python project for LLM call sites, harness waste and eval coverage."""
    from .scan import scan_project

    if fail_on not in (*SEVERITY_ORDER, "none"):
        raise typer.BadParameter("--fail-on must be one of high, medium, low, info, none")
    book = PriceBook.load(pricing if pricing else path)
    result = scan_project(path, book)

    if as_json:
        data = result.as_dict()
        data["root"] = str(path)  # keep output portable across machines
        typer.echo(json.dumps(data, indent=2, sort_keys=True))
    else:
        _print_scan(result)

    if fail_on != "none" and any(SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[fail_on] for f in result.findings):
        raise typer.Exit(1)


def _print_scan(result) -> None:
    sites = [s for s in result.call_sites if not s.is_test_file]
    covered = sum(1 for s in sites if result.coverage.get(s.id))
    typer.echo(f"Scanned {result.files_scanned} Python files · {len(sites)} LLM call sites · "
               f"{covered} covered by an eval")
    for err in result.errors:
        typer.echo(f"  ! could not parse {err}")

    if sites:
        typer.echo("\nCall sites")
        for s in sites:
            model = s.model.value if s.model.kind == "literal" else f"({s.model.kind})"
            system = f"~{s.system.tokens}-token system" if s.system.tokens else ""
            guard = "eval: " + ", ".join(result.coverage[s.id]) if result.coverage.get(s.id) else "no eval"
            typer.echo(f"  {s.id:<34} {s.provider:<9} {model:<18} {s.function or '-':<24} {system:<20} {guard}")

    if not result.findings:
        typer.echo("\nNo findings.")
        return
    counts = {sev: sum(1 for f in result.findings if f.severity == sev) for sev in SEVERITY_ORDER}
    summary = " · ".join(f"{n} {sev}" for sev, n in counts.items() if n)
    typer.echo(f"\nFindings ({len(result.findings)}: {summary})")
    ordered = sorted(result.findings, key=lambda f: (-SEVERITY_ORDER[f.severity], f.file, f.line, f.rule))
    for f in ordered:
        typer.echo(f"\n  {f.severity.upper():<6} {f.rule}  {f.file}:{f.line}  {f.title}")
        typer.echo(f"         {f.evidence}")
        typer.echo(f"         → {f.recommendation}")
        extras = []
        if f.est_saving:
            extras.append(f"est. saving: {f.est_saving}")
        if f.category != "coverage":
            extras.append("guard: " + (", ".join(f.guard) if f.guard else "none"))
        if extras:
            typer.echo("         " + " · ".join(extras))


@app.command()
def measure(
    logs: list[Path] = typer.Argument(..., exists=True, help="Usage log files or folders (JSONL/JSON/OTLP, .gz ok)"),
    outcomes: Path | None = typer.Option(None, exists=True, help="Extra outcomes file (CSV task_id,success or JSONL)"),
    scan_path: Path | None = typer.Option(None, "--scan", exists=True, file_okay=False,
                                          help="Also scan this project and price each finding with measured usage"),
    task_key: str | None = typer.Option(None, help="Field that groups requests into tasks (default: task_id, …)"),
    site_key: str | None = typer.Option(None, help="Field naming the call site (default: site, function, …)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    pricing: Path | None = PricingOpt,
):
    """Measure spend, cache hit rate and cost per successful task from usage logs."""
    from .measure import combined_saving, measure_findings
    from .usage import load_logs, load_outcomes_file
    from .usage.analyze import baseline

    book = PriceBook.load(pricing if pricing else scan_path)
    loaded = load_logs(list(logs), task_key, site_key)
    extra = load_outcomes_file(outcomes, task_key or "task_id") if outcomes else []
    base = baseline(loaded.records, loaded.outcomes + extra, book)

    measured = None
    if scan_path:
        from .scan import scan_project

        measured = measure_findings(scan_project(scan_path, book), base, book)

    if as_json:
        data = {"baseline": base.as_dict(), "load": {"files": loaded.files, "errors": loaded.errors,
                                                     "error_count": loaded.error_count,
                                                     "lines_without_usage": loaded.lines_without_usage}}
        if measured is not None:
            window, month = combined_saving(measured)
            data["findings"] = [m.as_dict() for m in measured]
            data["combined_saving"] = {"window": window, "month": month}
        typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))
        return
    _print_measure(base, loaded, measured)


def _money(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"${x:,.{digits}f}"


def _print_measure(base, loaded, measured) -> None:
    from .measure import combined_saving

    if loaded.error_count:
        typer.echo(f"! {loaded.error_count} unreadable lines/files, e.g. {loaded.errors[0]}")
    if not base.requests:
        typer.echo("No LLM usage found in the logs.")
        return
    f = base.monthly_factor
    span = (f"over {base.window_days:.1f} days ({base.window_start:%Y-%m-%d} → {base.window_end:%Y-%m-%d})"
            if base.window_days is not None else "(no timestamps)")
    month = f" · ~{_money(base.total.cost * f)} / month" if f else ""
    typer.echo(f"{base.requests:,} LLM requests {span} · {_money(base.total.cost)} total{month}")
    hit = base.total.tokens.cache_hit_rate
    typer.echo(f"Cache hit rate (share of input tokens read from cache): {hit:.1%}" if hit is not None else "")
    if base.unpriced_models:
        names = ", ".join(f"{k} ({v})" for k, v in base.unpriced_models.items())
        typer.echo(f"! Not priced (add to harnessdoc.pricing.json): {names}")

    typer.echo("")
    if base.tasks_with_outcome:
        sr = base.success_rate
        typer.echo(f"Tasks with an outcome: {base.tasks_with_outcome:,} · succeeded {base.successes:,} "
                   f"({sr.value:.1%}, 95% CI {sr.low:.1%}–{sr.high:.1%})")
        cps = base.cost_per_success
        if cps:
            typer.echo(f"Cost per successful task: {_money(cps.value, 4)}  (95% CI {_money(cps.low, 4)}–"
                       f"{_money(cps.high, 4)})")
        typer.echo(f"Cost per task: mean {_money(base.cost_per_task_mean, 4)} · median "
                   f"{_money(base.cost_per_task_median, 4)} · p90 {_money(base.cost_per_task_p90, 4)}")
        if base.requests_without_outcome:
            share = base.cost_without_outcome / base.total.cost if base.total.cost else 0
            typer.echo(f"Not in the per-task figures: {base.requests_without_outcome:,} requests without a task "
                       f"outcome, {_money(base.cost_without_outcome)} ({share:.0%} of spend) — e.g. batch jobs")
    else:
        typer.echo("No task outcomes in the logs, so cost per *successful* task can't be computed. Log outcomes "
                   "with UsageLogger.outcome(task_id, success) or pass --outcomes.")

    def table(title: str, groups: dict) -> None:
        typer.echo(f"\n{title}")
        for key, g in sorted(groups.items(), key=lambda kv: -kv[1].cost):
            share = g.cost / base.total.cost if base.total.cost else 0
            hit = g.tokens.cache_hit_rate
            typer.echo(f"  {key:<30} {g.requests:>7,} req  {_money(g.cost):>10}  {share:>5.0%}  "
                       f"cache hit {hit:.0%}" if hit is not None else f"  {key:<30} {g.requests:>7,} req")

    table("By model", base.by_model)
    table("By call site (as labelled in the logs)", base.by_site)

    if measured is None:
        return
    typer.echo("\nFindings priced with measured usage" + (" ($ per month)" if f else " ($ in log window)"))
    for m in measured:
        if not (m.saving or m.finding.rule in ("HD001", "HD003")):
            continue
        amount = m.saving_month if f else m.saving
        value = _money(amount) if amount else "  (enabler)"
        typer.echo(f"  {value:>12}  {m.finding.rule}  {m.finding.file}:{m.finding.line}  {m.finding.title}")
        typer.echo(f"                {m.basis}")
    window, month = combined_saving(measured)
    total = month if f and month is not None else window
    typer.echo(f"\n  Combined potential: {_money(total)}{' / month' if f else ''} "
               "(overlapping savings on the same call site combined, not added). Estimates until a "
               "before/after run proves them.")
    unmatched = [m for m in measured if m.site_key is None and m.finding.rule != "HD010"]
    if unmatched:
        typer.echo(f"  {len(unmatched)} findings had no matching usage in the logs (label requests with "
                   "site=<function name> to price them).")


@app.command()
def report(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Project root"),
    logs: list[Path] | None = typer.Option(None, "--logs", exists=True, help="Usage logs (default: goals file)"),
    outcomes: Path | None = typer.Option(None, exists=True, help="Outcomes file (default: goals file)"),
    out: Path = typer.Option(Path("harnessdoc-report.md"), "--out", "-o", help="Where to write the report"),
    stdout: bool = typer.Option(False, "--stdout", help="Print the report instead of writing a file"),
    pricing: Path | None = PricingOpt,
):
    """Write a Markdown report: goal vs baseline, ranked recommendations, eval coverage, method."""
    from .goals import GoalsError
    from .report import analyse, build_report

    try:
        a = analyse(path, logs, outcomes, book=PriceBook.load(pricing if pricing else path))
    except GoalsError as e:
        typer.echo(f"harnessdoc.goals.yaml: {e}", err=True)
        raise typer.Exit(2) from None
    text = build_report(a)
    if stdout:
        typer.echo(text, nl=False)
        return
    out.write_text(text, encoding="utf-8")
    typer.echo(f"Wrote {out} ({len(a.scan.findings)} findings"
               + (f", {a.base.requests:,} requests measured" if a.base else ", no usage logs") + ")")


goals_app = typer.Typer(help="Create or show harnessdoc.goals.yaml (what the app is for, what success means).",
                        no_args_is_help=True)
app.add_typer(goals_app, name="goals")


@goals_app.command("init")
def goals_init(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Project root"),
    app_desc: str = typer.Option(..., "--app", prompt="What does the app do (one sentence)?"),
    success: str = typer.Option(..., prompt="What counts as a successful task?"),
    monthly_tasks: int | None = typer.Option(None, help="Tasks per month"),
    target: float | None = typer.Option(None, help="Target cost per successful task, USD"),
    max_drop: float = typer.Option(1.0, help="Max success-rate drop a change may cause, percentage points"),
    task_key: str | None = typer.Option(None, help="Log field that groups requests into tasks"),
    logs: list[str] | None = typer.Option(None, "--logs", help="Usage log paths, relative to the project"),
    constraint: list[str] | None = typer.Option(None, "--constraint", help="Repeatable, e.g. 'stay on Anthropic'"),
    force: bool = typer.Option(False, help="Overwrite an existing goals file"),
):
    """Create harnessdoc.goals.yaml (prompts for the two required answers)."""
    from .goals import Goals, GoalsError, goals_path, save_goals

    target_path = goals_path(path)
    if target_path.exists() and not force:
        typer.echo(f"{target_path} already exists (use --force to overwrite).", err=True)
        raise typer.Exit(1)
    try:
        g = Goals(app=app_desc, success=success, monthly_tasks=monthly_tasks, target_cost_per_success=target,
                  max_success_rate_drop=max_drop, task_key=task_key, logs=list(logs or []),
                  constraints=list(constraint or []))
        written = save_goals(g, path)
    except GoalsError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    typer.echo(f"Wrote {written}")


@goals_app.command("show")
def goals_show(path: Path = typer.Argument(Path("."), exists=True, help="Project root or goals file")):
    """Validate and print the goals file."""
    from .goals import GoalsError, load_goals

    try:
        g = load_goals(path)
    except GoalsError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    if g is None:
        typer.echo("No harnessdoc.goals.yaml here. Create one with `harnessdoc goals init`.")
        raise typer.Exit(1)
    typer.echo(json.dumps(g.as_dict(), indent=2))


@app.command()
def mcp():
    """Run the harnessdoc MCP server on stdio (for Claude Code, Cursor, Codex, …)."""
    try:
        from .mcp_server import server
    except ImportError as e:
        typer.echo(f"The MCP server needs the 'mcp' extra: pip install 'harnessdoc[mcp]' ({e})", err=True)
        raise typer.Exit(2) from None
    server.run("stdio")


if __name__ == "__main__":
    app()
