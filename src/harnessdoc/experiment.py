"""Experiments: replay a call site's guard-eval cases on alternative setups with the user's own API key.

A spec (YAML) names the function to call, its cases and the variants to try:

    name: router-on-haiku
    call: acme_support.router:classify_intent      # module:function, imported from the project root
    cases: tests/evals/classify_intent_cases.jsonl # {"id", "input": {kwargs}, "expected"} per line
    check: exact                                   # exact | normalized | contains | regex
    repeats: 1
    variants:
      baseline: {}                                 # the first variant is the baseline
      haiku:
        model_overrides: {claude-opus-5-5: claude-haiku-5-5}
      cached:
        kwargs: {cache_control: {type: ephemeral}}

Each case runs through the user's real code; harnessdoc intercepts the SDK call to apply the variant and
record usage. Variants are interleaved per case. Spending stops at the budget. Every non-baseline variant
gets the same verdict as `harnessdoc compare` (SHIP / REJECT / INCONCLUSIVE / NO CLEAR SAVING).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .compare import compare
from .interceptor import BudgetExceeded, Call, Interceptor, Variant
from .pricing import PriceBook, UnknownModel
from .usage.records import Outcome, UsageRecord

CHECKS = ("exact", "normalized", "contains", "regex")


class ExperimentError(ValueError):
    pass


# ----------------------------------------------------------------------------- spec


@dataclass
class Spec:
    name: str
    call: str
    cases: str
    variants: list[Variant]
    check: str = "exact"
    repeats: int = 1
    max_success_rate_drop: float | None = None

    @property
    def function_name(self) -> str:
        return self.call.rsplit(":", 1)[-1]

    @classmethod
    def load(cls, path: str | Path) -> Spec:
        p = Path(path)
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ExperimentError(f"can't read {p}: {e}") from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Spec:
        if not isinstance(data, dict):
            raise ExperimentError("experiment spec must be a mapping")
        missing = [k for k in ("name", "call", "cases", "variants") if k not in data]
        if missing:
            raise ExperimentError(f"experiment spec is missing {missing}")
        if not re.fullmatch(r"[\w.]+:\w+", str(data["call"])):
            raise ExperimentError("`call` must look like package.module:function")
        variants_raw = data["variants"]
        if not isinstance(variants_raw, dict) or len(variants_raw) < 2:
            raise ExperimentError("`variants` needs at least two entries; the first is the baseline")
        variants = []
        allowed = {"model_overrides", "max_tokens", "kwargs", "env"}
        for name, cfg in variants_raw.items():
            cfg = cfg or {}
            extra = set(cfg) - allowed
            if extra:
                raise ExperimentError(f"variant {name!r}: unknown keys {sorted(extra)} (allowed: {sorted(allowed)})")
            variants.append(Variant(str(name), dict(cfg.get("model_overrides") or {}), cfg.get("max_tokens"),
                                    dict(cfg.get("kwargs") or {}), {k: str(v) for k, v in (cfg.get("env") or {}).items()}))
        check = str(data.get("check", "exact"))
        if check not in CHECKS:
            raise ExperimentError(f"`check` must be one of {CHECKS}")
        repeats = int(data.get("repeats", 1))
        if not 1 <= repeats <= 20:
            raise ExperimentError("`repeats` must be between 1 and 20")
        return cls(str(data["name"]), str(data["call"]), str(data["cases"]), variants, check, repeats,
                   data.get("max_success_rate_drop"))


def load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ExperimentError(f"cases file not found: {path}")
    cases = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError as e:
            raise ExperimentError(f"{path.name}:{i}: invalid JSON ({e.msg})") from e
        if not isinstance(c, dict) or not isinstance(c.get("input"), dict) or "expected" not in c:
            raise ExperimentError(f'{path.name}:{i}: each case needs "input" (an object of kwargs) and "expected"')
        c.setdefault("id", f"case-{i}")
        cases.append(c)
    if not cases:
        raise ExperimentError(f"{path.name} has no cases")
    return cases


def make_check(mode: str) -> Callable[[Any, Any], bool]:
    def norm(x: Any) -> str:
        return re.sub(r"\s+", " ", str(x)).strip().lower()

    return {
        "exact": lambda out, exp: out == exp,
        "normalized": lambda out, exp: norm(out) == norm(exp),
        "contains": lambda out, exp: norm(exp) in norm(out),
        "regex": lambda out, exp: re.search(str(exp), str(out)) is not None,
    }[mode]


# ----------------------------------------------------------------------------- running


@dataclass
class CaseResult:
    variant: str
    case_id: str
    repeat: int
    passed: bool
    cost: float
    calls: int
    unmeasured_calls: int
    error: str | None = None


@dataclass
class VariantSummary:
    name: str
    runs: int
    passed: int
    cost: float
    calls: int
    unmeasured_calls: int
    errors: int
    verdict: dict[str, Any] | None = None  # compare() result vs baseline


@dataclass
class ExperimentResult:
    name: str
    call: str
    budget_usd: float
    spent_usd: float
    stopped_early: bool
    variants: list[VariantSummary]
    cases: list[CaseResult]
    unpriced_models: list[str] = field(default_factory=list)
    sdk_targets_patched: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@contextmanager
def _project_on_path(root: Path):
    sys.path.insert(0, str(root))
    try:
        yield
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass


@contextmanager
def _env(values: dict[str, str]):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def import_function(root: Path, call: str) -> Callable:
    module, _, name = call.partition(":")
    with _project_on_path(root):
        try:
            mod = importlib.import_module(module)
        except Exception as e:
            raise ExperimentError(f"can't import {module}: {e.__class__.__name__}: {e}") from e
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise ExperimentError(f"{call} is not a function")
    return fn


def run_experiment(spec: Spec, root: str | Path, budget_usd: float, book: PriceBook | None = None,
                   max_cases: int | None = None, progress: Callable[[str], None] | None = None,
                   targets=None) -> ExperimentResult:
    if budget_usd <= 0:
        raise ExperimentError("budget must be a positive dollar amount")
    root = Path(root).resolve()
    book = book or PriceBook.load(root)
    cases = load_cases((root / spec.cases).resolve())[: max_cases or None]
    fn = import_function(root, spec.call)
    check = make_check(spec.check)

    spent = 0.0
    stopped = False
    unpriced: set[str] = set()
    results: list[CaseResult] = []
    per_variant: dict[str, tuple[list[UsageRecord], list[Outcome]]] = {v.name: ([], []) for v in spec.variants}
    patched = 0

    def budget_check() -> None:
        if spent >= budget_usd:
            raise BudgetExceeded(f"budget ${budget_usd:.2f} reached")

    for case in cases:
        for rep in range(spec.repeats):
            for variant in spec.variants:
                calls: list[Call] = []
                task = f"{case['id']}#{rep}"

                def on_call(c: Call, calls: list[Call] = calls) -> None:
                    nonlocal spent
                    calls.append(c)
                    if c.usage is not None:
                        try:
                            spent += book.cost(c.model, c.usage).total
                        except UnknownModel:
                            unpriced.add(c.model)

                error = None
                passed = False
                interceptor = Interceptor(variant, on_call, budget_check, **({"targets": targets} if targets else {}))
                try:
                    with _env(variant.env), _project_on_path(root), interceptor:
                        patched = max(patched, interceptor.patched)
                        out = fn(**case["input"])
                        if inspect.isawaitable(out):
                            out = asyncio.run(_await(out))
                        passed = bool(check(out, case["expected"]))
                except BudgetExceeded:
                    stopped = True
                except Exception as e:  # noqa: BLE001 — a crashing case is a failed case, not a crashed run
                    error = f"{e.__class__.__name__}: {str(e)[:200]}"

                if stopped:
                    break
                cost = 0.0
                for c in calls:
                    if c.usage is not None:
                        try:
                            cost += book.cost(c.model, c.usage).total
                        except UnknownModel:
                            pass
                        per_variant[variant.name][0].append(
                            UsageRecord(c.provider, c.model, c.usage, task_id=task, site=spec.function_name))
                per_variant[variant.name][1].append(Outcome(task, passed))
                results.append(CaseResult(variant.name, str(case["id"]), rep, passed, cost, len(calls),
                                          sum(1 for c in calls if c.usage is None), error))
                if progress:
                    progress(f"{variant.name:<12} {case['id']}#{rep}: {'pass' if passed else 'FAIL'} "
                             f"${cost:.4f}" + (f" ({error})" if error else ""))
            if stopped:
                break
        if stopped:
            break

    # drop the last partially-run case so every variant is compared on the same cases
    done = {}
    for r in results:
        done.setdefault((r.case_id, r.repeat), set()).add(r.variant)
    complete = {k for k, v in done.items() if len(v) == len(spec.variants)}
    results = [r for r in results if (r.case_id, r.repeat) in complete]
    keep_tasks = {f"{c}#{rep}" for c, rep in complete}
    for name, (recs, outs) in per_variant.items():
        per_variant[name] = ([r for r in recs if r.task_id in keep_tasks], [o for o in outs if o.task_id in keep_tasks])

    drop = spec.max_success_rate_drop if spec.max_success_rate_drop is not None else 1.0
    base_name = spec.variants[0].name
    summaries = []
    for v in spec.variants:
        rs = [r for r in results if r.variant == v.name]
        verdict = None
        if v.name != base_name and rs:
            verdict = compare(per_variant[base_name], per_variant[v.name], book, drop).as_dict()
        summaries.append(VariantSummary(v.name, len(rs), sum(r.passed for r in rs), sum(r.cost for r in rs),
                                        sum(r.calls for r in rs), sum(r.unmeasured_calls for r in rs),
                                        sum(1 for r in rs if r.error), verdict))
    return ExperimentResult(spec.name, spec.call, budget_usd, spent, stopped, summaries, results,
                            sorted(unpriced), patched)


async def _await(x):
    return await x


# ----------------------------------------------------------------------------- planning


def site_avg_cost(project: str | Path, function: str, book: PriceBook) -> tuple[float | None, str | None]:
    """Average measured cost per request at a call site and its most-used model, from the goals file's logs."""
    from .goals import load_goals
    from .usage import load_logs
    from .usage.analyze import baseline

    project = Path(project)
    goals = load_goals(project)
    if not goals or not goals.logs:
        return None, None
    paths = [project / p for p in goals.logs if (project / p).exists()]
    if not paths:
        return None, None
    recs = [r for r in load_logs(paths, goals.task_key).records if r.site == function]
    if not recs:
        return None, None
    b = baseline(recs, [], book)
    models = sorted(b.by_model, key=lambda m: -b.by_model[m].requests)
    return b.total.cost / b.requests, models[0] if models else None


@dataclass
class Plan:
    cases: int
    runs: int
    variants: list[str]
    est_cost_usd: float | None
    suggested_budget_usd: float | None
    basis: str


def plan_experiment(spec: Spec, root: str | Path, book: PriceBook | None = None,
                    avg_cost_per_call: float | None = None, baseline_model: str | None = None) -> Plan:
    """Count the runs and, when the call site's average cost per request is known (from usage logs), estimate
    the cost. Model overrides are scaled by list price. Nothing is called."""
    root = Path(root).resolve()
    book = book or PriceBook.load(root)
    cases = load_cases((root / spec.cases).resolve())
    runs = len(cases) * spec.repeats
    names = [v.name for v in spec.variants]
    if avg_cost_per_call is None:
        return Plan(len(cases), runs * len(names), names, None, None,
                    "no usage logs for this call site, so cost can't be estimated — set a small budget for a pilot run")
    est = 0.0
    for v in spec.variants:
        factor = 1.0
        target = v.model_overrides.get(baseline_model or "", v.model_overrides.get("*")) if baseline_model else None
        if target and baseline_model:
            try:
                a, b = book.price(baseline_model), book.price(target)
                factor = (b.input + b.output) / (a.input + a.output)
            except UnknownModel:
                pass
        est += runs * avg_cost_per_call * factor
    return Plan(len(cases), runs * len(names), names, est, math.ceil(est * 1.5 * 100) / 100,
                f"{runs} runs per variant × measured ${avg_cost_per_call:.5f} per call at this site "
                "(scaled by list price for model overrides; assumes one LLM call per case)")
