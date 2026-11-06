"""Before/after comparison: did a harness change make successful tasks cheaper without hurting quality?

Verdicts are deliberately conservative:

* Quality is a **non-inferiority** test. With d = success rate after − before and its 95% CI [lo, hi]
  (Newcombe hybrid score interval, built from Wilson intervals), and an allowed drop m from the goals file:
  PASS if lo > −m, FAIL if hi < −m, otherwise INCONCLUSIVE.
* Cost compares cost per successful task; the two windows are independent samples, so the variances of the
  two delta-method estimates add. LOWER if the CI of (after − before) is entirely below zero, HIGHER if
  entirely above, otherwise NO CLEAR CHANGE.
* SHIP needs both PASS and LOWER. REJECT if quality FAILs or cost is HIGHER. Anything else is
  INCONCLUSIVE (with how many tasks per window would settle it) or NO CLEAR SAVING.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .pricing import PriceBook
from .usage.analyze import Baseline, Interval, baseline, wilson
from .usage.records import Outcome, UsageRecord

Z95 = 1.959963984540054


@dataclass
class Arm:
    requests: int
    cost: float
    window_days: float | None
    tasks_with_outcome: int
    successes: int
    success_rate: Interval | None
    cost_per_success: Interval | None
    cache_hit_rate: float | None

    @classmethod
    def of(cls, b: Baseline) -> Arm:
        return cls(b.requests, b.total.cost, b.window_days, b.tasks_with_outcome, b.successes, b.success_rate,
                   b.cost_per_success, b.total.tokens.cache_hit_rate)


@dataclass
class SiteDelta:
    site: str
    before: float  # cost per successful task attributable to this site (or per day, see `unit`)
    after: float
    unit: str


@dataclass
class Comparison:
    before: Arm
    after: Arm
    margin: float  # allowed success-rate drop, as a fraction
    cost_change: Interval | None  # after − before, USD per successful task
    cost_change_pct: float | None
    success_change: Interval | None  # after − before, fraction
    quality: str  # PASS | FAIL | INCONCLUSIVE | UNKNOWN
    cost: str  # LOWER | HIGHER | NO CLEAR CHANGE | UNKNOWN
    verdict: str  # SHIP | REJECT | INCONCLUSIVE | NO CLEAR SAVING
    tasks_needed_per_window: int | None
    reasons: list[str] = field(default_factory=list)
    sites: list[SiteDelta] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- statistics


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> Interval | None:
    """CI for p2 − p1 (Newcombe 1998, method 10: hybrid score from two Wilson intervals)."""
    w1, w2 = wilson(k1, n1, z), wilson(k2, n2, z)
    if w1 is None or w2 is None:
        return None
    p1, p2 = w1.value, w2.value
    d = p2 - p1
    lower = d - math.sqrt((p2 - w2.low) ** 2 + (w1.high - p1) ** 2)
    upper = d + math.sqrt((w2.high - p2) ** 2 + (p1 - w1.low) ** 2)
    return Interval(d, lower, upper)


def _se(ci: Interval) -> float:
    return (ci.high - ci.low) / (2 * Z95)


def tasks_needed(p1: float, p2: float, margin: float, z: float = Z95) -> int | None:
    """Tasks per window so that, at the observed rates, the CI's lower bound clears −margin.
    None when the observed drop already exceeds the margin (more data can't make it pass)."""
    gap = (p2 - p1) + margin
    if gap <= 0:
        return None
    return math.ceil(z * z * (p1 * (1 - p1) + p2 * (1 - p2)) / (gap * gap))


# ----------------------------------------------------------------------------- comparison


def _filter(records: list[UsageRecord], site: str | None) -> list[UsageRecord]:
    if not site:
        return records
    return [r for r in records if r.site and (r.site == site or r.site.endswith("." + site)
                                               or r.site.endswith("/" + site))]


def compare(before: tuple[list[UsageRecord], list[Outcome]], after: tuple[list[UsageRecord], list[Outcome]],
            book: PriceBook, max_success_rate_drop: float = 1.0, site: str | None = None) -> Comparison:
    """`max_success_rate_drop` is in percentage points (as in the goals file)."""
    b = baseline(_filter(before[0], site), before[1], book)
    a = baseline(_filter(after[0], site), after[1], book)
    margin = max_success_rate_drop / 100
    reasons: list[str] = []

    # quality ---------------------------------------------------------------
    success = newcombe_diff(b.successes, b.tasks_with_outcome, a.successes, a.tasks_with_outcome)
    needed = None
    if success is None:
        quality = "UNKNOWN"
        reasons.append("No task outcomes in one of the windows, so quality can't be compared.")
    elif success.low > -margin:
        quality = "PASS"
        reasons.append(f"Success rate changed by {success.value * 100:+.1f} pt (95% CI {success.low * 100:+.1f} to "
                       f"{success.high * 100:+.1f}); the worst case stays within the allowed "
                       f"−{max_success_rate_drop:g} pt.")
    elif success.high < -margin:
        quality = "FAIL"
        reasons.append(f"Success rate dropped by {-success.value * 100:.1f} pt (95% CI {success.low * 100:+.1f} to "
                       f"{success.high * 100:+.1f}) — beyond the allowed −{max_success_rate_drop:g} pt.")
    else:
        quality = "INCONCLUSIVE"
        needed = tasks_needed(b.success_rate.value, a.success_rate.value, margin)  # type: ignore[union-attr]
        more = (f" About {needed:,} tasks per window would settle it at the observed rates."
                if needed else " At the observed rates more data is unlikely to make it pass.")
        reasons.append(f"Success rate changed by {success.value * 100:+.1f} pt, but the 95% CI "
                       f"({success.low * 100:+.1f} to {success.high * 100:+.1f}) still allows a drop beyond "
                       f"−{max_success_rate_drop:g} pt.{more}")

    # cost ------------------------------------------------------------------
    cb, ca = b.cost_per_success, a.cost_per_success
    cost_change = pct = None
    if cb is None or ca is None:
        cost = "UNKNOWN"
        reasons.append("Cost per successful task is missing in one of the windows (no outcomes or no successes).")
    else:
        diff = ca.value - cb.value
        se = math.sqrt(_se(cb) ** 2 + _se(ca) ** 2) if not (math.isnan(cb.low) or math.isnan(ca.low)) else math.nan
        cost_change = Interval(diff, diff - Z95 * se, diff + Z95 * se)
        pct = diff / cb.value if cb.value else None
        if cost_change.high < 0:
            cost = "LOWER"
        elif cost_change.low > 0:
            cost = "HIGHER"
        else:
            cost = "NO CLEAR CHANGE"
        reasons.append(f"Cost per successful task: ${cb.value:.4f} → ${ca.value:.4f} "
                       f"({(pct or 0) * 100:+.1f}%, 95% CI of the change ${cost_change.low:+.4f} to "
                       f"${cost_change.high:+.4f}).")

    # verdict ---------------------------------------------------------------
    if quality == "FAIL" or cost == "HIGHER":
        verdict = "REJECT"
    elif quality == "PASS" and cost == "LOWER":
        verdict = "SHIP"
    elif quality == "PASS" and cost == "NO CLEAR CHANGE":
        verdict = "NO CLEAR SAVING"
    else:
        verdict = "INCONCLUSIVE"

    return Comparison(Arm.of(b), Arm.of(a), margin, cost_change, pct, success, quality, cost, verdict, needed,
                      reasons, _site_deltas(b, a))


def _site_deltas(b: Baseline, a: Baseline) -> list[SiteDelta]:
    """Per call-site cost, normalised so windows of different size compare: per successful task when both
    windows have successes, else per day, else raw totals."""
    if b.successes and a.successes:
        nb, na, unit = b.successes, a.successes, "per successful task"
    elif b.window_days and a.window_days and b.window_days >= 1 and a.window_days >= 1:
        nb, na, unit = b.window_days, a.window_days, "per day"
    else:
        nb = na = 1
        unit = "total"
    out = []
    for site in sorted(set(b.by_site) | set(a.by_site)):
        before = b.by_site[site].cost / nb if site in b.by_site else 0.0
        after = a.by_site[site].cost / na if site in a.by_site else 0.0
        out.append(SiteDelta(site, before, after, unit))
    out.sort(key=lambda s: s.after - s.before)
    return out
