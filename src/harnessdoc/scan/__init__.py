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


GUARD_MARKER = re.compile(r"#\s*harnessdoc:guard\s+cases=(\S+)")


def _empty_guard_scaffold(root: Path, test_file: str, text: str) -> bool:
    """A generated guard eval whose cases file is still empty does not count as coverage."""
    m = GUARD_MARKER.search(text)
    if not m:
        return False
    cases = (root / test_file).parent / m.group(1)
    try:
        return not any(line.strip() for line in cases.read_text(encoding="utf-8").splitlines())
    except OSError:
        return True


def eval_coverage(root: Path, sites: list[CallSite], files: list[str],
                  call_graph: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """Map each call site to the test/eval files that exercise it.

    A test covers a call site when it references the call's enclosing function, or any project function
    that (transitively) calls it — tests usually drive the top-level handler, not the LLM wrapper.
    Matching is by function name, so two same-named functions can over-report coverage.
    """
    graph = call_graph or {}
    tests = {}
    for f in files:
        if f.endswith(".py") and is_test_path(f):
            try:
                text = (root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _empty_guard_scaffold(root, f, text):
                tests[f] = text

    # which project functions does each test reach?
    reach: dict[str, set[str]] = {}
    for f, text in tests.items():
        frontier = {name for name in graph if re.search(rf"\b{re.escape(name)}\b", text)}
        seen = set(frontier)
        while frontier:
            nxt = {c for n in frontier for c in graph.get(n, ()) if c not in seen}
            seen |= nxt
            frontier = nxt
        reach[f] = seen

    cov: dict[str, list[str]] = {}
    for s in sites:
        if s.is_test_file:
            continue
        if not s.function:
            cov[s.id] = []
            continue
        rx = re.compile(rf"\b{re.escape(s.function)}\b")
        cov[s.id] = sorted(f for f, text in tests.items() if s.function in reach.get(f, ()) or rx.search(text))
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
    graph: dict[str, set[str]] = {}
    for m in modules:
        if not is_test_path(m.rel):
            for fn, callees in m.call_graph().items():
                graph.setdefault(fn, set()).update(callees)
    coverage = eval_coverage(root, sites, files, graph)

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
