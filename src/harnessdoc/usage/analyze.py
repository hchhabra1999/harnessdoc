"""Baseline: what the app spends, where, and what each successful task costs.

Definition used throughout harnessdoc:

    cost per successful task = (total cost of tasks with a known outcome) / (number of successful tasks)

Failed tasks still cost money; that spend is overhead every success has to carry, so it stays in the
numerator. The 95% interval uses the delta method for a ratio of means over tasks.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..pricing import PriceBook, UnknownModel
from .records import Outcome, UsageRecord

Z95 = 1.959963984540054
DAYS_PER_MONTH = 30.0


@dataclass
class Tokens:
    uncached_input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    def add(self, r: UsageRecord) -> None:
        self.uncached_input += r.usage.uncached_input
        self.cache_read += r.usage.cache_read
        self.cache_write += r.usage.cache_write
        self.output += r.usage.output

    @property
    def input_total(self) -> int:
        return self.uncached_input + self.cache_read + self.cache_write

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cache_read / self.input_total if self.input_total else None


@dataclass
class Group:
    key: str
    requests: int = 0
    tokens: Tokens = field(default_factory=Tokens)
    cost: float = 0.0
    unpriced_requests: int = 0
    models: set[str] = field(default_factory=set)

    def as_dict(self, monthly_factor: float | None) -> dict:
        return {
            "key": self.key,
            "requests": self.requests,
            "tokens": asdict(self.tokens) | {"cache_hit_rate": self.tokens.cache_hit_rate},
            "cost": self.cost,
            "cost_per_month": self.cost * monthly_factor if monthly_factor else None,
            "unpriced_requests": self.unpriced_requests,
            "models": sorted(self.models),
        }


@dataclass
class Interval:
    value: float
    low: float
    high: float


@dataclass
class Baseline:
    requests: int
    total: Group
    by_model: dict[str, Group]
    by_site: dict[str, Group]
    window_start: datetime | None
    window_end: datetime | None
    unpriced_models: dict[str, int]
    tasks: int
    tasks_with_ids: bool
    tasks_with_outcome: int
    successes: int
    success_rate: Interval | None
    cost_per_task_mean: float | None
    cost_per_task_median: float | None
    cost_per_task_p90: float | None
    cost_per_success: Interval | None
    requests_without_outcome: int = 0
    cost_without_outcome: float = 0.0  # spend outside the cost-per-success figure (e.g. batch jobs)

    @property
    def window_days(self) -> float | None:
        if not self.window_start or not self.window_end:
            return None
        return (self.window_end - self.window_start).total_seconds() / 86400

    @property
    def monthly_factor(self) -> float | None:
        """Multiply window cost by this to get a 30-day projection (None if the window is under a day)."""
        d = self.window_days
        return DAYS_PER_MONTH / d if d and d >= 1 else None

    def as_dict(self) -> dict:
        f = self.monthly_factor
        return {
            "requests": self.requests,
            "window": {
                "start": self.window_start.isoformat() if self.window_start else None,
                "end": self.window_end.isoformat() if self.window_end else None,
                "days": self.window_days,
            },
            "total": self.total.as_dict(f),
            "by_model": {k: g.as_dict(f) for k, g in sorted(self.by_model.items())},
            "by_site": {k: g.as_dict(f) for k, g in sorted(self.by_site.items())},
            "unpriced_models": dict(sorted(self.unpriced_models.items())),
            "tasks": {
                "count": self.tasks,
                "ids_present": self.tasks_with_ids,
                "with_outcome": self.tasks_with_outcome,
                "successes": self.successes,
                "success_rate": asdict(self.success_rate) if self.success_rate else None,
                "cost_per_task": {"mean": self.cost_per_task_mean, "median": self.cost_per_task_median,
                                  "p90": self.cost_per_task_p90},
                "cost_per_success": asdict(self.cost_per_success) if self.cost_per_success else None,
                "requests_without_outcome": self.requests_without_outcome,
                "cost_without_outcome": self.cost_without_outcome,
            },
        }


# ----------------------------------------------------------------------------- statistics


def wilson(k: int, n: int, z: float = Z95) -> Interval | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return Interval(p, max(0.0, c - h), min(1.0, c + h))


def ratio_interval(costs: list[float], successes: list[int], z: float = Z95) -> Interval | None:
    """Delta-method CI for sum(costs)/sum(successes) over i.i.d. tasks."""
    n = len(costs)
    s = sum(successes)
    if n == 0 or s == 0:
        return None
    r = sum(costs) / s
    if n < 2:
        return Interval(r, math.nan, math.nan)
    mc, ms = statistics.fmean(costs), statistics.fmean(successes)
    vc = statistics.variance(costs)
    vs = statistics.variance(successes)
    cov = sum((c - mc) * (x - ms) for c, x in zip(costs, successes, strict=True)) / (n - 1)
    var_r = max(0.0, (vc - 2 * r * cov + r * r * vs) / (n * ms * ms))
    h = z * math.sqrt(var_r)
    return Interval(r, max(0.0, r - h), r + h)


def _percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return math.nan
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# ----------------------------------------------------------------------------- baseline


def baseline(records: list[UsageRecord], outcomes: list[Outcome], book: PriceBook) -> Baseline:
    total = Group("total")
    by_model: dict[str, Group] = {}
    by_site: dict[str, Group] = {}
    unpriced: dict[str, int] = defaultdict(int)
    task_cost: dict[str, float] = defaultdict(float)
    task_requests: dict[str, int] = defaultdict(int)
    record_outcome: dict[str, bool] = {}
    ts = [r.ts for r in records if r.ts]
    ids_present = any(r.task_id for r in records)

    for i, r in enumerate(records):
        try:
            cost = book.cost(r.model, r.usage, on=r.ts.date() if r.ts else None).total
            priced = True
        except UnknownModel:
            cost, priced = 0.0, False
            unpriced[r.model or "(no model)"] += 1
        resolved = book.resolve_id(r.model) or r.model or "(no model)"
        for g in (total,
                  by_model.setdefault(resolved, Group(resolved)),
                  by_site.setdefault(r.site or "(untagged)", Group(r.site or "(untagged)"))):
            g.requests += 1
            g.tokens.add(r)
            g.cost += cost
            g.unpriced_requests += 0 if priced else 1
            g.models.add(resolved)
        tid = r.task_id or f"request:{i}"
        task_cost[tid] += cost
        task_requests[tid] += 1
        if r.success is not None:
            record_outcome[tid] = r.success

    outcome_by_task = dict(record_outcome)
    for o in sorted(outcomes, key=lambda o: (o.ts is None, o.ts or datetime.min)):
        outcome_by_task[o.task_id] = o.success  # latest outcome wins

    known = [t for t in task_cost if t in outcome_by_task]
    unknown = [t for t in task_cost if t not in outcome_by_task]
    successes = sum(1 for t in known if outcome_by_task[t])
    # per-task cost stats describe real tasks (those with outcomes) when any exist, else every request group
    costs = [task_cost[t] for t in known] if known else list(task_cost.values())

    return Baseline(
        requests=len(records),
        total=total,
        by_model=by_model,
        by_site=by_site,
        window_start=min(ts) if ts else None,
        window_end=max(ts) if ts else None,
        unpriced_models=dict(unpriced),
        tasks=len(task_cost),
        tasks_with_ids=ids_present,
        tasks_with_outcome=len(known),
        successes=successes,
        success_rate=wilson(successes, len(known)),
        cost_per_task_mean=statistics.fmean(costs) if costs else None,
        cost_per_task_median=statistics.median(costs) if costs else None,
        cost_per_task_p90=_percentile(costs, 0.9) if costs else None,
        cost_per_success=ratio_interval([task_cost[t] for t in known],
                                        [1 if outcome_by_task[t] else 0 for t in known]),
        requests_without_outcome=sum(task_requests[t] for t in unknown),
        cost_without_outcome=sum(task_cost[t] for t in unknown),
    )
