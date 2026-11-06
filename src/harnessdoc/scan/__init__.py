"""Static scan of a Python project: LLM call sites, harness checks and eval coverage."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..pricing import PriceBook
from .checks import CHECKS, coverage_findings
from .model import CallSite, Finding, ScanResult
from .pyast import ModuleAnalyzer, Project, is_test_path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "site-packages", "__pycache__", "build", "dist",
             ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".claude"}

__all__ = ["CallSite", "Finding", "ScanResult", "scan_project"]


def _walk(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for f in sorted(filenames):
            out.append(str((Path(dirpath) / f).relative_to(root)))
    return out


def eval_coverage(root: Path, sites: list[CallSite], files: list[str]) -> dict[str, list[str]]:
    """Map each call site to the test/eval files that reference its enclosing function by name."""
    tests = {}
    for f in files:
        if f.endswith(".py") and is_test_path(f):
            try:
                tests[f] = (root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    cov: dict[str, list[str]] = {}
    for s in sites:
        if s.is_test_file:
            continue
        if not s.function:
            cov[s.id] = []
            continue
        rx = re.compile(rf"\b{re.escape(s.function)}\b")
        cov[s.id] = sorted(f for f, text in tests.items() if rx.search(text))
    return cov


def scan_project(root: str | Path, book: PriceBook | None = None) -> ScanResult:
    root = Path(root).resolve()
    book = book or PriceBook.load(root)
    files = _walk(root)
    project = Project()
    errors: list[str] = []
    modules = []
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            mod = ModuleAnalyzer(root / f, f, files, project)
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            errors.append(f"{f}: {e.__class__.__name__}: {e}")
            continue
        project.add(mod)
        modules.append(mod)

    sites = [s for m in modules for s in m.call_sites()]
    sites.sort(key=lambda s: (s.file, s.line))
    coverage = eval_coverage(root, sites, files)

    findings: list[Finding] = []
    for s in sites:
        if s.is_test_file:
            continue
        for check in CHECKS:
            for f in check(s, book):
                f.guard = coverage.get(s.id, [])
                findings.append(f)
    findings += coverage_findings(sites, coverage)

    seen = set()
    unique = []
    for f in sorted(findings, key=lambda f: (f.file, f.line, f.rule)):
        key = (f.rule, f.file, f.line)
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return ScanResult(str(root), sum(1 for f in files if f.endswith(".py")), sites, unique, coverage, errors)
