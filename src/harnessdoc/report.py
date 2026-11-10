"""Deterministic Markdown report: goal vs baseline, ranked recommendations, what to guard first.

Same inputs → byte-identical report (no "generated at" timestamp; the price-book date and log window are
stated instead), so the report can be committed and diffed in a PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .goals import Goals, load_goals
from .measure import MeasuredFinding, combined_saving, measure_findings
from .pricing import PriceBook, fmt_usd
from .scan import ScanResult, scan_project
from .scan.pyast import is_non_product_path
from .usage import load_logs, load_outcomes_file
from .usage.analyze import Baseline, baseline

SEVERITY = {"high": 3, "medium": 2, "low": 1, "info": 0}
ENABLERS = {"HD001", "HD003"}  # fixes whose saving is counted on HD002 for the same call


@dataclass
class Analysis:
    root: Path
    book: PriceBook
    goals: Goals | None
    scan: ScanResult
    base: Baseline | None
    measured: list[MeasuredFinding] | None
    log_files: list[str]
    log_errors: int


def analyse(root: str | Path, logs: list[str | Path] | None = None, outcomes: str | Path | None = None,
            goals: Goals | None = None, book: PriceBook | None = None) -> Analysis:
    root = Path(root).resolve()
    book = book or PriceBook.load(root)
    goals = goals if goals is not None else load_goals(root)
    if not logs and goals and goals.logs:
        logs = [root / p for p in goals.logs]
    if outcomes is None and goals and goals.outcomes:
        outcomes = root / goals.outcomes
    task_key = goals.task_key if goals else None

    scan = scan_project(root, book)
    base = measured = None
    files: list[str] = []
    errors = 0
    paths = [Path(p) for p in (logs or []) if Path(p).exists()]
    if paths:
        loaded = load_logs(paths, task_key)
        extra = load_outcomes_file(Path(outcomes), task_key or "task_id") if outcomes and Path(outcomes).exists() \
            else []
        base = baseline(loaded.records, loaded.outcomes + extra, book)
        measured = measure_findings(scan, base, book)
        files = [_rel(f, root) for f in loaded.files]
        errors = loaded.error_count
    return Analysis(root, book, goals, scan, base, measured, files, errors)


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return Path(path).name


def _money(x: float | None, digits: int = 2) -> str:
    return fmt_usd(x, digits)


def ranked(a: Analysis) -> list[tuple]:
    """(finding, measured-or-None) ordered by measured saving, then severity; info-level coverage excluded."""
    measured = {(m.finding.rule, m.finding.file, m.finding.line): m for m in (a.measured or [])}
    items = []
    for f in a.scan.findings:
        if f.rule == "HD010":
            continue
        m = measured.get((f.rule, f.file, f.line))
        saving = (m.saving_month if m and m.saving_month is not None else (m.saving if m else None)) or 0
        items.append((f, m, saving))
    items.sort(key=lambda t: (-t[2], -SEVERITY[t[0].severity], t[0].file, t[0].line, t[0].rule))
    return items


def build_report(a: Analysis) -> str:
    out: list[str] = []
    w = out.append
    g = a.goals
    sites = [s for s in a.scan.call_sites if not s.is_test_file]
    covered = [s for s in sites if a.scan.coverage.get(s.id)]
    monthly = a.base.monthly_factor if a.base else None

    w(f"# harnessdoc report — {g.app if g else a.root.name}\n")
    w(f"Price book as of {a.book.as_of} · {a.scan.files_scanned} Python files · {len(sites)} LLM call sites · "
      f"{len(covered)} covered by an eval" + (f" · logs: {', '.join(a.log_files)}" if a.log_files else "") + "\n")

    # ------------------------------------------------------------------ goal
    w("## Goal\n")
    if g:
        rows = [("What the app does", g.app), ("A successful task", g.success or "—"),
                ("Quality bar", g.quality_bar or f"success rate may drop ≤ {g.max_success_rate_drop:g} pt"),
                ("Target cost per successful task", _money(g.target_cost_per_success, 4)
                 if g.target_cost_per_success else "—"),
                ("Monthly volume", f"{g.monthly_tasks:,} tasks" if g.monthly_tasks else "—"),
                ("Priorities", ", ".join(g.priorities) or "—")]
        w("| | |\n|---|---|")
        for k, v in rows:
            w(f"| {k} | {v} |")
        if g.constraints:
            w("\n**Constraints** (recommendations that conflict with these should be skipped):")
            for c in g.constraints:
                w(f"- {c}")
        w("")
    else:
        w("_No `harnessdoc.goals.yaml` yet — run `/harnessdoc` or `harnessdoc goals init` so the report knows "
          "what counts as success._\n")

    # ------------------------------------------------------------------ baseline
    w("## Baseline\n")
    b = a.base
    if b and b.requests:
        cps = b.cost_per_success
        sr = b.success_rate
        w("| Metric | Value |\n|---|---|")
        span = f"{b.window_days:.1f} days ({b.window_start:%Y-%m-%d} → {b.window_end:%Y-%m-%d})" \
            if b.window_days is not None else "no timestamps"
        w(f"| Window | {span} |")
        w(f"| LLM requests | {b.requests:,} |")
        w(f"| Spend | {_money(b.total.cost)}" + (f" (~{_money(b.total.cost * monthly)} / month)" if monthly else "")
          + " |")
        hit = b.total.tokens.cache_hit_rate
        w(f"| Cache hit rate (input tokens) | {hit:.1%} |" if hit is not None else "| Cache hit rate | — |")
        if sr:
            w(f"| Success rate | {sr.value:.1%} (95% CI {sr.low:.1%}–{sr.high:.1%}, n={b.tasks_with_outcome:,}) |")
        if cps:
            w(f"| **Cost per successful task** | **{_money(cps.value, 4)}** (95% CI {_money(cps.low, 4)}–"
              f"{_money(cps.high, 4)}) |")
            if g and g.target_cost_per_success:
                gap = cps.value / g.target_cost_per_success - 1
                status = "meets target" if gap <= 0 else f"{gap:.0%} above target"
                w(f"| Target | {_money(g.target_cost_per_success, 4)} — {status} |")
        if b.requests_without_outcome:
            w(f"| Spend outside task outcomes | {_money(b.cost_without_outcome)} "
              f"({b.requests_without_outcome:,} requests, e.g. batch jobs) |")
        if b.unpriced_models:
            w(f"| Unpriced models | {', '.join(sorted(b.unpriced_models))} |")
        w("")
    else:
        w("_No usage logs given, so savings below are structural estimates, not measured dollars. Add `logs:` to "
          "the goals file or pass `--logs`._\n")

    # ------------------------------------------------------------------ recommendations
    w("## Recommendations\n")
    items = ranked(a)
    if not items:
        w("No harness waste found by the static checks.\n")
    else:
        per_site_enablers: dict[str, list[str]] = {}
        for f, _, _ in items:
            if f.rule in ENABLERS and f.call_site:
                per_site_enablers.setdefault(f.call_site, []).append(f"{f.rule} ({f.file}:{f.line})")
        unit = "/ month" if monthly else "in log window" if b else "estimate"
        w(f"| # | Change | Where | Saving ({unit}) | Guarded by | Do first |")
        w("|---|---|---|---|---|---|")
        for i, (f, m, saving) in enumerate(items, 1):
            if m and saving:
                money = _money(saving)
            elif f.rule in ENABLERS:
                money = "enables HD002"
            elif f.est_saving_pct:
                money = f"~{f.est_saving_pct:.0%} of call"
            else:
                money = "—"
            guard = ", ".join(f"`{x}`" for x in f.guard) if f.guard else "**none**"
            first = ", ".join(per_site_enablers.get(f.call_site or "", [])) if f.rule == "HD002" else ""
            # only changes that can alter outputs need a guard eval first; caching and batching don't
            if not f.guard and f.rule in ("HD006", "HD008"):
                first = ("write a guard eval; " + first).rstrip("; ")
            w(f"| {i} | {f.title} | `{f.file}:{f.line}` | {money} | {guard} | {first or '—'} |")
        if a.measured:
            window, month = combined_saving(a.measured)
            total = month if month is not None and monthly else window
            current = b.total.cost * monthly if b and monthly else (b.total.cost if b else None)
            share = f" — {total / current:.0%} of current spend" if current else ""
            w(f"\n**Combined potential:** {_money(total)} {unit}{share}. Savings on the same call site are "
              "combined, not added. These are estimates until a before/after run measures them.\n")
        else:
            w("")

    # ------------------------------------------------------------------ guard first
    uncovered = [s for s in sites if not a.scan.coverage.get(s.id) and not is_non_product_path(s.file)]
    w("## Before changing anything: eval coverage\n")
    if uncovered:
        w(f"{len(uncovered)} of {len(sites)} LLM call sites have no eval, so a change to them can't be shown to "
          "keep quality. Add a small guard eval (10–30 real inputs with expected outputs) first:\n")
        for s in uncovered:
            w(f"- `{s.id}` — `{s.function or '?'}` ({s.provider}, "
              f"{s.model.value if s.model.kind == 'literal' else 'model not static'})")
        w("")
    else:
        w("Every LLM call site is referenced by a test or eval.\n")

    # ------------------------------------------------------------------ details
    w("## Findings in detail\n")
    for f in sorted(a.scan.findings, key=lambda f: (-SEVERITY[f.severity], f.file, f.line, f.rule)):
        w(f"### {f.rule} · {f.severity} · `{f.file}:{f.line}` — {f.title}\n")
        w(f"{f.evidence}\n")
        w(f"**Fix:** {f.recommendation}\n")
        if f.est_saving:
            w(f"**Estimated saving:** {f.est_saving}\n")
    w("## Call sites\n")
    w("| Call site | Provider | Model | Function | Static prompt | Eval |\n|---|---|---|---|---|---|")
    for s in sites:
        model = s.model.value if s.model.kind == "literal" else f"({s.model.kind})"
        prompt = f"~{s.system.tokens} tokens" if s.system.tokens else "—"
        ev = ", ".join(f"`{x}`" for x in a.scan.coverage.get(s.id, [])) or "none"
        w(f"| `{s.id}` | {s.provider} | `{model}` | `{s.function or '-'}` | {prompt} | {ev} |")

    # ------------------------------------------------------------------ method
    w("\n## How these numbers were computed\n")
    w("- Static checks parse the code without running it; token sizes of prompts are estimated at ~4 characters "
      "per token.")
    w("- Prices come from harnessdoc's price book (as of the date above) or `harnessdoc.pricing.json`.")
    w("- Cost per successful task = spend on tasks with a known outcome ÷ successful tasks (failed tasks' spend "
      "counts against the successes). 95% CI by the delta method over tasks; success rate by Wilson interval.")
    w("- Savings for a model swap assume the same token counts on the smaller model — an upper bound until an "
      "experiment measures it.")
    if a.log_errors:
        w(f"- {a.log_errors} log lines could not be read and were skipped.")
    return "\n".join(out).rstrip() + "\n"


def write_report(a: Analysis, out: str | Path) -> Path:
    path = Path(out)
    path.write_text(build_report(a), encoding="utf-8")
    return path
