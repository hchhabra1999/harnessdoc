"""`harnessdoc` command-line interface."""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer

from . import __version__
from .pricing import PriceBook, UnknownModel, Usage, fmt_usd

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
    return fmt_usd(x, digits)


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


@app.command()
def compare(
    before: list[Path] = typer.Option(..., "--before", exists=True, help="Usage logs before the change"),
    after: list[Path] = typer.Option(..., "--after", exists=True, help="Usage logs after the change"),
    project: Path = typer.Option(Path("."), exists=True, file_okay=False, help="Project root (for goals/prices)"),
    max_drop: float | None = typer.Option(None, help="Allowed success-rate drop in points (default: goals file, 1)"),
    site: str | None = typer.Option(None, help="Only compare requests from this call site / function"),
    task_key: str | None = typer.Option(None, help="Field that groups requests into tasks"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    require_ship: bool = typer.Option(False, help="Exit 1 unless the verdict is SHIP (for CI)"),
    pricing: Path | None = PricingOpt,
):
    """Before/after: did a change lower cost per successful task without hurting quality?"""
    from .compare import compare as run_compare
    from .goals import GoalsError, load_goals
    from .usage import load_logs

    try:
        goals = load_goals(project)
    except GoalsError as e:
        typer.echo(f"harnessdoc.goals.yaml: {e}", err=True)
        raise typer.Exit(2) from None
    drop = max_drop if max_drop is not None else (goals.max_success_rate_drop if goals else 1.0)
    key = task_key or (goals.task_key if goals else None)
    book = PriceBook.load(pricing if pricing else project)
    b, a = load_logs(list(before), key), load_logs(list(after), key)
    c = run_compare((b.records, b.outcomes), (a.records, a.outcomes), book, drop, site)

    if as_json:
        typer.echo(json.dumps(c.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        _print_compare(c, drop, site)
    if c.verdict == "REJECT" or (require_ship and c.verdict != "SHIP"):
        raise typer.Exit(1)


def _ci(iv, pct: bool = False, digits: int = 4) -> str:
    if iv is None:
        return "—"
    if pct:
        return f"{iv.value:.1%} ({iv.low:.1%}–{iv.high:.1%})"
    return f"{fmt_usd(iv.value, digits)} ({fmt_usd(iv.low, digits)}–{fmt_usd(iv.high, digits)})"


def _print_compare(c, drop: float, site: str | None) -> None:
    scope = f" · call site `{site}` only" if site else ""
    typer.echo(f"Verdict: {c.verdict}   (quality {c.quality} vs allowed −{drop:g} pt · cost {c.cost}){scope}\n")
    rows = [
        ("Window", *(f"{x.window_days:.1f} days" if x.window_days is not None else "—" for x in (c.before, c.after))),
        ("LLM requests", f"{c.before.requests:,}", f"{c.after.requests:,}"),
        ("Spend", f"${c.before.cost:,.2f}", f"${c.after.cost:,.2f}"),
        ("Spend / 30 days", *(f"${x.cost / x.window_days * 30:,.2f}" if x.window_days and x.window_days >= 1
                              else "—" for x in (c.before, c.after))),
        ("Cache hit rate", *(f"{x.cache_hit_rate:.1%}" if x.cache_hit_rate is not None else "—"
                             for x in (c.before, c.after))),
        ("Success rate (95% CI)", _ci(c.before.success_rate, True), _ci(c.after.success_rate, True)),
        ("Cost per successful task", _ci(c.before.cost_per_success), _ci(c.after.cost_per_success)),
    ]
    typer.echo(f"  {'':<26} {'before':<28} {'after':<28}")
    for label, x, y in rows:
        typer.echo(f"  {label:<26} {x:<28} {y:<28}")
    typer.echo("\nWhy")
    for r in c.reasons:
        typer.echo(f"  - {r}")
    if c.sites:
        unit = c.sites[0].unit
        typer.echo(f"\nBy call site (spend {unit})")
        for s in c.sites:
            change = f"{(s.after / s.before - 1):+.0%}" if s.before else "new"
            typer.echo(f"  {s.site:<30} {fmt_usd(s.before, 5)} → {fmt_usd(s.after, 5)}  {change}")


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


@app.command("check")
def check_cmd(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Project root"),
    baseline: Path | None = typer.Option(None, help="Baseline file; only findings not in it count as new"),
    write_baseline: Path | None = typer.Option(None, help="Record current findings as the baseline and exit"),
    fail_on: str = typer.Option("medium", help="Exit 1 if a NEW finding is at or above: high|medium|low|info|none"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    pricing: Path | None = PricingOpt,
):
    """CI gate: fail on new harness findings only (existing ones live in a committed baseline)."""
    from .check import check as run_check
    from .check import read_baseline
    from .check import write_baseline as save_baseline
    from .scan import scan_project

    if fail_on not in (*SEVERITY_ORDER, "none"):
        raise typer.BadParameter("--fail-on must be one of high, medium, low, info, none")
    result = scan_project(path, PriceBook.load(pricing if pricing else path))
    if write_baseline:
        save_baseline(result, write_baseline)
        typer.echo(f"Baseline with {len(result.findings)} findings written to {write_baseline}. Commit it.")
        return
    try:
        known = read_baseline(baseline) if baseline else None
    except (OSError, ValueError, KeyError) as e:
        typer.echo(f"Can't read baseline: {e}", err=True)
        raise typer.Exit(2) from None
    res = run_check(result, known)
    failing = res.failing(fail_on)
    if as_json:
        typer.echo(json.dumps(res.as_dict(fail_on), indent=2, sort_keys=True))
    else:
        label = "new" if baseline else "found (no baseline)"
        typer.echo(f"harnessdoc check: {len(res.new)} {label} · {len(res.unchanged)} unchanged · "
                   f"{len(res.fixed)} fixed since baseline")
        for f in res.new:
            mark = "✗" if f in failing else "·"
            typer.echo(f"  {mark} {f.severity.upper():<6} {f.rule}  {f.file}:{f.line}  {f.title}")
        for fp in res.fixed:
            typer.echo(f"  ✓ fixed  {fp}")
        if failing:
            typer.echo(f"\nFailing: {len(failing)} new finding(s) at or above '{fail_on}'. Fix them, or if they are "
                       "accepted, refresh the baseline with --write-baseline.")
    if failing:
        raise typer.Exit(1)


guard_app = typer.Typer(help="Guard evals: small tests that must keep passing when a call gets cheaper.",
                        no_args_is_help=True)
app.add_typer(guard_app, name="guard")


@guard_app.command("scaffold")
def guard_scaffold(
    site: str = typer.Argument(..., help="Call site id (file.py:line) or enclosing function name"),
    path: Path = typer.Option(Path("."), "--project", exists=True, file_okay=False, help="Project root"),
    tests_dir: str = typer.Option("tests/evals", help="Where to put the guard eval, relative to the project"),
):
    """Write a pytest guard eval + empty cases file for an LLM call site (never overwrites)."""
    from .guard import GuardError, scaffold

    try:
        s = scaffold(path, site, tests_dir)
    except GuardError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    root = path.resolve()
    if not s.created:
        typer.echo(f"Guard eval for {s.site.id} already exists: {s.test_file.relative_to(root)}")
        return
    for f in s.created:
        typer.echo(f"Created {f.relative_to(root)}")
    typer.echo(f"Next: add 10–30 real cases to {s.cases_file.relative_to(root)}; the call site counts as "
               "covered once it has at least one.")


@app.command()
def sessions(
    directory: Path = typer.Option(None, "--dir", help="Claude Code logs folder (default: ~/.claude/projects)"),
    since: str | None = typer.Option(None, help="Only from this date (2026-11-01) or last N days (7d)"),
    until: str | None = typer.Option(None, help="Only before this date (2026-11-08)"),
    cwd: str | None = typer.Option(None, help="Only sessions run under this directory"),
    top: int = typer.Option(8, help="How many of the costliest sessions to list"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    pricing: Path | None = PricingOpt,
):
    """What your Claude Code usage costs at API list prices, and where the tokens go.

    Reads only token counts, models, timestamps and session ids from the session logs — never your prompts,
    replies or tool output. Dollars are API-equivalent: a subscription plan doesn't bill per token.
    """
    from .sessions import DEFAULT_ROOT, analyse, parse_since

    root = (directory or DEFAULT_ROOT).expanduser()
    if not root.exists():
        typer.echo(f"No Claude Code logs at {root}. Pass --dir if they live elsewhere.", err=True)
        raise typer.Exit(2)
    try:
        s_dt = parse_since(since) if since else None
        u_dt = parse_since(until) if until else None
    except ValueError:
        typer.echo("--since/--until take a date like 2026-11-01, or a number of days like 7d.", err=True)
        raise typer.Exit(2) from None
    r = analyse(root, PriceBook.load(pricing), s_dt, u_dt, cwd, top)
    if as_json:
        typer.echo(json.dumps(r.as_dict(), indent=2, sort_keys=True, default=str))
        return
    _print_sessions(r)


def _print_sessions(r) -> None:
    if not r.messages:
        typer.echo(f"No Claude Code usage found ({r.files} log files read).")
        return
    c = r.total.cost
    span = f"{r.window[0]:%Y-%m-%d} → {r.window[1]:%Y-%m-%d}" if r.window else "unknown dates"
    typer.echo(f"{r.messages:,} assistant messages · {span} · {r.files} log files ({r.subagent_files} subagent)")
    typer.echo(f"API-equivalent cost at list prices: {_money(c.total)}   (a subscription plan is not billed this way)")
    typer.echo(f"  de-duplicated {r.duplicate_records:,} repeated log records (each message is logged once per content block)")
    if r.unreadable_lines:
        typer.echo(f"  skipped {r.unreadable_lines:,} unreadable log lines")
    typer.echo("\nWhere the money goes")
    for label, v in (("cache reads", c.cache_read), ("output", c.output), ("cache writes, 1-hour", c.cache_write_1h),
                     ("cache writes, 5-minute", c.cache_write_5m), ("uncached input", c.uncached_input)):
        typer.echo(f"  {label:<24} {_money(v):>12}  {v / c.total:>5.0%}" if c.total else f"  {label}")
    hit = r.total.cache_hit_rate
    typer.echo(f"  cache hit rate (input tokens read from cache): {hit:.1%}" if hit is not None else "")
    if r.subagent.messages:
        typer.echo(f"  subagents: {r.subagent.messages:,} messages, {_money(r.subagent.cost.total)} "
                   f"({r.subagent.cost.total / c.total:.0%} of cost)")

    typer.echo("\nBy model")
    for key, g in sorted(r.by_model.items(), key=lambda kv: -kv[1].cost.total):
        typer.echo(f"  {key:<22} {g.messages:>7,} msgs  {_money(g.cost.total):>11}  {g.cost.total / c.total:>5.0%}")
    days = sorted(r.by_day.items())[-7:]
    typer.echo("\nLast days")
    for day, g in days:
        typer.echo(f"  {day}  {g.messages:>6,} msgs  {_money(g.cost.total):>10}")
    typer.echo("\nCostliest sessions")
    for s in r.sessions:
        project = (s.project or "?").replace(str(Path.home()), "~")
        sub = f" (subagents {_money(s.subagent_cost)})" if s.subagent_cost else ""
        typer.echo(f"  {s.session[:8]}  {_money(s.cost):>10}{sub}  {s.messages:,} msgs  {', '.join(s.models)}  {project}")
    if r.unpriced_models:
        typer.echo(f"\n! Not priced (add to harnessdoc.pricing.json): {', '.join(r.unpriced_models)}")
    if r.fast_unpriced:
        typer.echo(f"! {r.fast_unpriced} fast-mode messages were priced at the standard rate (no fast-mode price on file).")


experiment_app = typer.Typer(help="Replay guard-eval cases on alternative setups (uses your API key; costs money).",
                             no_args_is_help=True)
app.add_typer(experiment_app, name="experiment")


def _site_avg_cost(project: Path, function: str, book: PriceBook) -> tuple[float | None, str | None]:
    from .experiment import site_avg_cost

    return site_avg_cost(project, function, book)


def _load_spec(spec_path: Path):
    from .experiment import ExperimentError, Spec

    try:
        return Spec.load(spec_path)
    except ExperimentError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None


@experiment_app.command("plan")
def experiment_plan(
    spec_path: Path = typer.Argument(Path("harnessdoc.experiment.yaml"), exists=True, help="Experiment spec"),
    project: Path = typer.Option(Path("."), exists=True, file_okay=False, help="Project root"),
):
    """Count runs and estimate cost. Calls nothing."""
    from .experiment import ExperimentError, plan_experiment

    spec = _load_spec(spec_path)
    book = PriceBook.load(project)
    avg, model = _site_avg_cost(project, spec.function_name, book)
    try:
        p = plan_experiment(spec, project, book, avg, model)
    except ExperimentError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    _print_plan(spec, p)


def _print_plan(spec, p) -> None:
    typer.echo(f"Experiment '{spec.name}' on {spec.call}")
    typer.echo(f"  {p.cases} cases × {spec.repeats} repeat(s) × {len(p.variants)} variants = {p.runs} runs "
               f"(baseline: {p.variants[0]}; candidates: {', '.join(p.variants[1:])})")
    if p.est_cost_usd is not None:
        typer.echo(f"  Estimated cost: {fmt_usd(p.est_cost_usd, 4)} — suggested --budget {p.suggested_budget_usd:.2f}")
    typer.echo(f"  Basis: {p.basis}")


@experiment_app.command("run")
def experiment_run(
    spec_path: Path = typer.Argument(Path("harnessdoc.experiment.yaml"), exists=True, help="Experiment spec"),
    budget: float = typer.Option(..., help="Hard spending cap in USD; the run stops before exceeding it"),
    project: Path = typer.Option(Path("."), exists=True, file_okay=False, help="Project root"),
    max_cases: int | None = typer.Option(None, help="Only run the first N cases (pilot)"),
    out: Path | None = typer.Option(None, help="Results file (default: harnessdoc-experiment-<name>.json)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
    quiet: bool = typer.Option(False, help="Don't print each case"),
    allow_unpriced: bool = typer.Option(False, "--allow-unpriced",
                                        help="Keep running when a model has no price (the budget then can't be enforced)"),
):
    """Run the experiment with your API keys (from the environment) and print a verdict per variant."""
    from .experiment import ExperimentError, plan_experiment, run_experiment

    spec = _load_spec(spec_path)
    book = PriceBook.load(project)
    avg, model = _site_avg_cost(project, spec.function_name, book)
    try:
        p = plan_experiment(spec, project, book, avg, model)
    except ExperimentError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    _print_plan(spec, p)
    typer.echo(f"  Budget: ${budget:.2f} (hard cap)")
    if not yes and not typer.confirm("This calls the LLM APIs with your keys and costs money. Run it?"):
        raise typer.Exit(1)
    try:
        r = run_experiment(spec, project, budget, book, max_cases, None if quiet else (lambda s: typer.echo("  " + s)),
                           allow_unpriced=allow_unpriced)
    except ExperimentError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None

    target = out or project / f"harnessdoc-experiment-{re.sub(r'[^A-Za-z0-9_.-]', '-', spec.name)}.json"
    target.write_text(json.dumps(r.as_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
    typer.echo(f"\nSpent {fmt_usd(r.spent_usd, 4)} of ${r.budget_usd:.2f}")
    if r.stopped_early:
        typer.echo(f"! Stopped early: {r.stop_reason}")
    if r.sdk_targets_patched == 0:
        typer.echo("! No provider SDK was found to intercept — install your app's SDK in this environment.")
    if r.unpriced_models:
        typer.echo(f"! Unpriced models (cost shown as $0): {', '.join(r.unpriced_models)}")
    typer.echo(f"\n  {'variant':<14} {'pass':>9} {'cost':>10} {'errors':>7}  verdict vs {r.variants[0].name}")
    for v in r.variants:
        verdict = v.verdict["verdict"] if v.verdict else "(baseline)"
        pct = f"{v.passed}/{v.runs}" if v.runs else "—"
        typer.echo(f"  {v.name:<14} {pct:>9} {fmt_usd(v.cost, 4):>10} {v.errors:>7}  {verdict}")
    for v in r.variants:
        if v.verdict:
            typer.echo(f"\n{v.name}:")
            for reason in v.verdict["reasons"]:
                typer.echo(f"  - {reason}")
    typer.echo(f"\nResults: {target}")


@app.command()
def mcp():
    """Run the harnessdoc MCP server on stdio (for Claude Code, Cursor, Codex, …)."""
    try:
        from .mcp_server import server
    except ImportError as e:
        typer.echo(f"The MCP server needs the 'mcp' extra: pip install 'harnessdoc[mcp]' ({e})", err=True)
        raise typer.Exit(2) from None
    server.run("stdio")


skill_app = typer.Typer(help="The /harnessdoc skill for Claude Code (ships inside this package).",
                        no_args_is_help=True)
app.add_typer(skill_app, name="skill")


@skill_app.command("install")
def skill_install(
    directory: Path = typer.Option(Path.home() / ".claude" / "skills", "--dir",
                                   help="Skills folder (default: ~/.claude/skills)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing, different harnessdoc skill"),
):
    """Copy the /harnessdoc skill into Claude Code's skills folder, then type /harnessdoc in Claude Code."""
    from importlib import resources

    text = resources.files("harnessdoc").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    target = directory.expanduser() / "harnessdoc" / "SKILL.md"
    if target.exists():
        if target.read_text(encoding="utf-8") == text:
            typer.echo(f"Already installed and up to date: {target}")
            return
        if not force:
            typer.echo(f"{target} exists and differs from this version. Re-run with --force to replace it.",
                       err=True)
            raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    typer.echo(f"Installed {target}")
    typer.echo("Next: `claude mcp add harnessdoc -- harnessdoc mcp`, then type /harnessdoc in Claude Code.")


if __name__ == "__main__":
    app()
