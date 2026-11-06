"""Price book and cost engine.

Prices live in data/pricing.json (packaged) and can be overridden per project with a
``harnessdoc.pricing.json`` next to the code being analysed. All prices are USD per 1M tokens.

Token counts are always passed *split*: uncached input, cache reads, cache writes and output are
disjoint. Providers report them differently — Anthropic's ``input_tokens`` already excludes cached
tokens, while OpenAI's ``input_tokens`` and Gemini's ``prompt_token_count`` include them — so use the
``Usage.from_*`` constructors rather than building ``Usage`` from raw provider fields by hand.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

PER = 1_000_000


def fmt_usd(x: float | None, digits: int = 2, signed: bool = False) -> str:
    """Format dollars at `digits` decimals, widening to 3 significant digits when that would hide the value.

    LLM calls cost fractions of a cent, so a fixed `$0.0000` turns a real $0.000038 into nothing. Amounts that
    round cleanly (the usual case) print exactly as `digits` says; only values where rounding would lose more
    than 5% get extra places. `signed` adds an explicit + or - (for changes).
    """
    if x is None or math.isnan(x):
        return "—"
    sign = ("-" if x < 0 else "+" if signed else "") if x else ""
    a = abs(x)
    places = digits
    if a and abs(round(a, digits) - a) / a > 0.05:
        places = min(10, max(digits, 2 - math.floor(math.log10(a))))
    return f"{sign}${a:,.{places}f}"


_PREFIXES = ("anthropic.", "anthropic/", "openai/", "gemini/", "google/", "models/", "vertex_ai/")
_SUFFIXES = (
    re.compile(r"@.*$"),  # Vertex: claude-opus-4-5@20251101
    re.compile(r"-\d{8}$"),  # dated snapshot: -20251101
    re.compile(r"-\d{4}-\d{2}-\d{2}$"),  # dated snapshot: -2026-04-01
    re.compile(r"-latest$"),
    re.compile(r"-v\d+(:\d+)?$"),  # Bedrock: -v1:0
)


# ----------------------------------------------------------------------------- data classes


@dataclass(frozen=True)
class Usage:
    """Token counts for one request (or a sum of requests). All fields are disjoint."""

    uncached_input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_ttl: str | None = None  # e.g. "5m", "1h", "30m", "12h"; None = provider default
    batch: bool = False
    requests: int = 1

    @property
    def total_input(self) -> int:
        return self.uncached_input + self.cache_read + self.cache_write

    def __add__(self, other: Usage) -> Usage:
        if self.batch != other.batch or self.cache_write_ttl != other.cache_write_ttl:
            raise ValueError("can only add Usage with the same batch flag and cache TTL")
        return Usage(
            self.uncached_input + other.uncached_input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
            self.cache_write_ttl,
            self.batch,
            self.requests + other.requests,
        )

    @classmethod
    def from_anthropic(cls, input_tokens: int, output_tokens: int, cache_read_input_tokens: int = 0,
                       cache_creation_input_tokens: int = 0, **kw: Any) -> Usage:
        # Anthropic: input_tokens is already the uncached remainder.
        return cls(input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, **kw)

    @classmethod
    def from_openai(cls, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
                    cache_write_tokens: int = 0, **kw: Any) -> Usage:
        # OpenAI: input_tokens includes cached reads and writes.
        uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
        return cls(uncached, output_tokens, cached_tokens, cache_write_tokens, **kw)

    @classmethod
    def from_gemini(cls, prompt_token_count: int, candidates_token_count: int,
                    cached_content_token_count: int = 0, thoughts_token_count: int = 0, **kw: Any) -> Usage:
        # Gemini: prompt_token_count includes cached content; thinking tokens bill as output.
        uncached = max(0, prompt_token_count - cached_content_token_count)
        return cls(uncached, candidates_token_count + thoughts_token_count, cached_content_token_count, 0, **kw)


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input: float
    output: float
    cache_read: float | None
    cache_write_multipliers: dict[str, float]
    default_ttl: str | None
    cache_writes_charged: bool
    batch_multiplier: float
    min_cache_tokens: int | None
    long_context: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Cost:
    model: str
    uncached_input: float
    cache_read: float
    cache_write: float
    output: float
    long_context: bool
    batch: bool

    @property
    def total(self) -> float:
        return self.uncached_input + self.cache_read + self.cache_write + self.output

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "uncached_input": self.uncached_input,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "output": self.output,
            "total": self.total,
            "long_context": self.long_context,
            "batch": self.batch,
        }


class UnknownModel(KeyError):
    pass


# ----------------------------------------------------------------------------- price book


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def normalise_model_id(model: str) -> str:
    m = model.strip().lower()
    for p in _PREFIXES:
        m = m.removeprefix(p)
    return m


class PriceBook:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.as_of: str = data.get("as_of", "unknown")
        self._index: dict[str, str] = {}  # model id -> provider
        for provider, pdata in data["providers"].items():
            for mid in pdata.get("models", {}):
                self._index[mid.lower()] = provider
            for alias in pdata.get("aliases", {}):
                self._index[alias.lower()] = provider

    @classmethod
    def load(cls, override: str | Path | None = None) -> PriceBook:
        base = json.loads(resources.files("harnessdoc.data").joinpath("pricing.json").read_text("utf-8"))
        if override is not None:
            p = Path(override)
            if p.is_dir():
                p = p / "harnessdoc.pricing.json"
            if p.exists():
                base = _deep_merge(base, json.loads(p.read_text("utf-8")))
        return cls(base)

    @property
    def models(self) -> list[str]:
        return sorted(self._index)

    def resolve_id(self, model: str) -> str | None:
        """Map an id as written in code (prefixes, dated snapshots, aliases) to a price-book id."""
        m = normalise_model_id(model)
        if m in self._index:
            return self._alias_target(m)
        for pattern in _SUFFIXES:
            stripped = pattern.sub("", m)
            if stripped != m and stripped in self._index:
                return self._alias_target(stripped)
        return None

    def _alias_target(self, mid: str) -> str:
        provider = self._index[mid]
        aliases = {k.lower(): v for k, v in self.data["providers"][provider].get("aliases", {}).items()}
        return aliases.get(mid, mid)

    def price(self, model: str, on: date | None = None) -> ModelPrice:
        mid = self.resolve_id(model)
        if mid is None:
            raise UnknownModel(model)
        provider = self._index[mid]
        pdata = self.data["providers"][provider]
        mdata = dict(pdata["models"][mid])

        on = on or date.today()
        for step in sorted(mdata.get("schedule", []), key=lambda s: s["from"]):
            if date.fromisoformat(step["from"]) <= on:
                mdata.update({k: v for k, v in step.items() if k != "from"})

        caching = pdata.get("caching", {})
        read = mdata.get("cache_read", "default")
        if read == "default":
            read = round(mdata["input"] * caching.get("default_read_multiplier", 1.0), 6)

        if "cache_writes" in mdata:
            writes_charged = bool(mdata["cache_writes"])
        else:
            writes_charged = bool(caching.get("write_multipliers"))

        min_cache = mdata.get("min_cache_tokens", caching.get("min_cache_tokens_default"))
        return ModelPrice(
            provider=provider,
            model=mid,
            input=float(mdata["input"]),
            output=float(mdata["output"]),
            cache_read=None if read is None else float(read),
            cache_write_multipliers=dict(caching.get("write_multipliers", {})),
            default_ttl=caching.get("default_ttl"),
            cache_writes_charged=writes_charged,
            batch_multiplier=float(mdata.get("batch_multiplier", pdata.get("batch_multiplier", 1.0))),
            min_cache_tokens=min_cache if isinstance(min_cache, int) else None,
            long_context=mdata.get("long_context"),
            raw=mdata,
        )

    def cost(self, model: str, usage: Usage, on: date | None = None) -> Cost:
        p = self.price(model, on)
        inp, out, read = p.input, p.output, p.cache_read
        long_ctx = bool(p.long_context and usage.total_input / max(usage.requests, 1)
                        > p.long_context["threshold_input_tokens"])
        if long_ctx:
            lc = p.long_context or {}
            inp, out = lc.get("input", inp), lc.get("output", out)
            read = lc.get("cache_read", read)

        if read is None:  # model has no discounted cache reads: bill reads as input
            read = inp
        if p.cache_writes_charged and usage.cache_write:
            ttl = usage.cache_write_ttl or p.default_ttl
            if ttl not in p.cache_write_multipliers:
                raise ValueError(f"{p.model}: unknown cache TTL {ttl!r}; "
                                 f"expected one of {sorted(p.cache_write_multipliers)}")
            write_rate = inp * p.cache_write_multipliers[ttl]
        else:
            write_rate = inp  # implicit caching: writes bill as ordinary input

        m = p.batch_multiplier if usage.batch else 1.0
        return Cost(
            model=p.model,
            uncached_input=usage.uncached_input * inp / PER * m,
            cache_read=usage.cache_read * read / PER * m,
            cache_write=usage.cache_write * write_rate / PER * m,
            output=usage.output * out / PER * m,
            long_context=long_ctx,
            batch=usage.batch,
        )
