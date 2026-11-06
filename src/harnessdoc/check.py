"""CI gate: fail only on *new* findings, so existing debt doesn't block every pull request.

    harnessdoc check . --write-baseline harnessdoc-baseline.json     # once, commit the file
    harnessdoc check . --baseline harnessdoc-baseline.json --fail-on medium   # in CI

Findings are matched by fingerprint = rule + file + enclosing function (not line number), so editing
unrelated lines doesn't make old findings look new. Multiplicity is kept: a second identical finding in the
same function is new.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .scan import ScanResult
from .scan.model import Finding

SEVERITY = {"info": 0, "low": 1, "medium": 2, "high": 3}
BASELINE_VERSION = 1


def fingerprint(f: Finding, functions: dict[str, str | None]) -> str:
    fn = functions.get(f.call_site or "", None) or "-"
    return f"{f.rule}:{f.file}:{fn}"


def _functions(scan: ScanResult) -> dict[str, str | None]:
    return {s.id: s.function for s in scan.call_sites}


def baseline_doc(scan: ScanResult) -> dict:
    fns = _functions(scan)
    items = sorted(
        ({"fingerprint": fingerprint(f, fns), "rule": f.rule, "severity": f.severity, "file": f.file,
          "line": f.line, "title": f.title} for f in scan.findings),
        key=lambda d: (d["fingerprint"], d["line"]),
    )
    return {"harnessdoc_baseline": BASELINE_VERSION, "findings": items}


def write_baseline(scan: ScanResult, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(baseline_doc(scan), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def read_baseline(path: str | Path) -> Counter:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("harnessdoc_baseline") != BASELINE_VERSION:
        raise ValueError(f"{path} is not a harnessdoc baseline (version {BASELINE_VERSION})")
    return Counter(d["fingerprint"] for d in data.get("findings", []))


@dataclass
class CheckResult:
    new: list[Finding] = field(default_factory=list)
    unchanged: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)  # fingerprints in the baseline that no longer occur

    def failing(self, fail_on: str) -> list[Finding]:
        if fail_on == "none":
            return []
        return [f for f in self.new if SEVERITY[f.severity] >= SEVERITY[fail_on]]

    def as_dict(self, fail_on: str) -> dict:
        return {
            "new": [f.as_dict() for f in self.new],
            "unchanged": len(self.unchanged),
            "fixed": self.fixed,
            "failing": [f"{f.rule} {f.file}:{f.line}" for f in self.failing(fail_on)],
        }


def check(scan: ScanResult, baseline: Counter | None) -> CheckResult:
    fns = _functions(scan)
    remaining = Counter(baseline or {})
    res = CheckResult()
    for f in sorted(scan.findings, key=lambda f: (f.file, f.line, f.rule)):
        fp = fingerprint(f, fns)
        if remaining[fp] > 0:
            remaining[fp] -= 1
            res.unchanged.append(f)
        else:
            res.new.append(f)
    res.fixed = sorted(fp for fp, n in remaining.items() for _ in range(n))
    return res
