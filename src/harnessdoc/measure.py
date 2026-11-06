"""Join a static scan with measured usage: each finding gets its call site's real spend and a $/month saving."""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import PER, PriceBook, UnknownModel
from .scan.model import CallSite, Finding, ScanResult
from .usage.analyze import Baseline, Group


@dataclass
class MeasuredFinding:
    finding: Finding
    site_key: str | None  # usage-log site label that matched the call site
    site_cost: float | None  # spend in the log window
    site_cost_month: float | None
    saving: float | None  # in the log window
    saving_month: float | None  # 30-day projection (None if the window is shorter than a day)
    basis: str  # how the saving was computed, in words

    def as_dict(self) -> dict:
        return {
            "finding": self.finding.as_dict(),
            "site_key": self.site_key,
            "site_cost": self.site_cost,
            "site_cost_month": self.site_cost_month,
            "saving": self.saving,
            "saving_month": self.saving_month,
            "basis": self.basis,
        }


def match_site(label: str, sites: list[CallSite]) -> CallSite | None:
    """Usage-log site label -> call site. Accepts 'file.py:line', 'function', or 'pkg.module.function'."""
    exact = [s for s in sites if s.id == label]
    if exact:
        return exact[0]
    fn = label.rsplit(".", 1)[-1] if ":" not in label else None
    if fn:
        by_fn = [s for s in sites if s.function == fn]
        if len(by_fn) == 1:
            return by_fn[0]
    if ":" in label:
        path, _, line = label.rpartition(":")
        cands = [s for s in sites if (s.file.endswith(path) or path.endswith(s.file)) and str(s.line) == line]
        if len(cands) == 1:
            return cands[0]
    return None


def site_groups(base: Baseline, sites: list[CallSite]) -> dict[str, tuple[str, Group]]:
    """call-site id -> (log label, usage group)."""
    out: dict[str, tuple[str, Group]] = {}
    for label, group in base.by_site.items():
        site = match_site(label, sites)
        if site and site.id not in out:
            out[site.id] = (label, group)
    return out


def _prefix_cache_saving(site: CallSite, group: Group, book: PriceBook) -> tuple[float | None, str]:
    model = site.model.value if site.model.kind == "literal" else None
    prefix = site.system.tokens or 0
    if not model or not prefix:
        return None, "static prefix size or model unknown"
    try:
        p = book.price(model)
    except UnknownModel:
        return None, f"no price for {model}"
    read = p.cache_read if p.cache_read is not None else p.input
    eligible = min(prefix * group.requests, group.tokens.uncached_input)
    saving = eligible * (p.input - read) / PER
    return saving, (f"~{prefix} static tokens × {group.requests} requests moved from input "
                    f"(${p.input:g}/1M) to cache reads (${read:g}/1M); excludes the occasional cache write")


def measure_findings(scan: ScanResult, base: Baseline, book: PriceBook) -> list[MeasuredFinding]:
    groups = site_groups(base, scan.call_sites)
    by_id = {s.id: s for s in scan.call_sites}
    factor = base.monthly_factor
    out = []
    for f in scan.findings:
        site = by_id.get(f.call_site or "")
        label, group = groups.get(f.call_site or "", (None, None))
        cost = group.cost if group else None
        month = cost * factor if cost is not None and factor else None
        saving, basis = None, "no usage logged for this call site"
        if group and site:
            if f.rule == "HD002":
                saving, basis = _prefix_cache_saving(site, group, book)
            elif f.rule in ("HD001", "HD003"):
                basis = "unlocks HD002's caching saving on the same call (counted there)"
            elif f.est_saving_pct is not None:
                saving = cost * f.est_saving_pct
                basis = f"{f.est_saving_pct:.0%} of this call site's measured spend"
            else:
                basis = "no direct price effect"
        saving_month = saving * factor if saving is not None and factor else None
        out.append(MeasuredFinding(f, label, cost, month, saving, saving_month, basis))
    out.sort(key=lambda m: (-(m.saving or 0), m.finding.file, m.finding.line, m.finding.rule))
    return out


def combined_saving(measured: list[MeasuredFinding]) -> tuple[float, float | None]:
    """Total potential saving (window, month) without double counting.

    Savings on the same call site overlap (a cheaper model *and* the batch API is not 99% + 50%), so per
    site they combine multiplicatively: cost × (1 − Π(1 − share_i)), each share capped at 100%.
    """
    per_site: dict[str, list[MeasuredFinding]] = {}
    for m in measured:
        if m.saving and m.site_cost and m.finding.call_site:
            per_site.setdefault(m.finding.call_site, []).append(m)
    window = 0.0
    month: float | None = 0.0
    for items in per_site.values():
        cost = items[0].site_cost or 0.0
        keep = 1.0
        for m in items:
            keep *= 1 - min(1.0, (m.saving or 0) / cost)
        window += cost * (1 - keep)
        if items[0].site_cost_month is None:
            month = None
        elif month is not None:
            month += (items[0].site_cost_month or 0) * (1 - keep)
    return window, month
