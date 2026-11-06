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


if __name__ == "__main__":
    app()
