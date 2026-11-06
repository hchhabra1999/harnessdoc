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


if __name__ == "__main__":
    app()
