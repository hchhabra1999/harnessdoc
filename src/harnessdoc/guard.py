"""Scaffold a guard eval for an LLM call site that has none.

A guard eval is a small set of real inputs with expected outputs. It turns "the cheaper setup seems fine"
into a test that fails when it isn't. The scaffold is a pytest file plus an empty JSONL cases file; until the
cases file has at least one case, harnessdoc does not count the call site as covered.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .scan import scan_project
from .scan.model import CallSite
from .scan.pyast import module_name

TEMPLATE = '''"""Guard eval for `{function}` ({site_id}) — scaffolded by harnessdoc.

Add 10–30 real cases to {cases_name}, one JSON object per line:
    {{"id": "short-name", "input": {{{input_example}}}, "expected": <what a correct answer looks like>}}
Until the cases file has at least one case, harnessdoc does not count this call site as covered.
"""
# harnessdoc:guard cases={cases_name}

import json
from pathlib import Path

import pytest

from {module} import {function}

CASES_FILE = Path(__file__).with_name("{cases_name}")
CASES = [json.loads(line) for line in CASES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def check(result, expected) -> None:
    """Default: exact match. If the output is not stable word-for-word, replace this with a looser check
    (normalised text, "contains", a regex, a JSON schema, or an LLM judge validated against human labels)."""
    assert result == expected


@pytest.mark.skipif(not CASES, reason="add cases to {cases_name}")
@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c.get("id", "")))
def test_{function}_guard(case):
    check({function}(**case["input"]), case["expected"])
'''


@dataclass
class Scaffold:
    site: CallSite
    test_file: Path
    cases_file: Path
    created: list[Path]


class GuardError(ValueError):
    pass


def find_site(root: Path, which: str) -> CallSite:
    sites = [s for s in scan_project(root).call_sites if not s.is_test_file]
    exact = [s for s in sites if s.id == which]
    if exact:
        return exact[0]
    by_fn = [s for s in sites if s.function == which]
    if len(by_fn) == 1:
        return by_fn[0]
    if len(by_fn) > 1:
        raise GuardError(f"{which!r} matches {len(by_fn)} call sites; pass one of: {[s.id for s in by_fn]}")
    raise GuardError(f"no LLM call site {which!r}; known: {[s.id for s in sites]}")


def _params(root: Path, site: CallSite) -> list[str]:
    tree = ast.parse((root / site.file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == site.function:
            names = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
            return [n for n in names if n not in ("self", "cls")]
    return []


def scaffold(root: str | Path, which: str, tests_dir: str = "tests/evals") -> Scaffold:
    root = Path(root).resolve()
    site = find_site(root, which)
    if not site.function:
        raise GuardError(f"{site.id} is not inside a function, so there is nothing for a test to call")
    out_dir = (root / tests_dir).resolve()
    if root not in out_dir.parents and out_dir != root:
        raise GuardError(f"{tests_dir} is outside the project")
    out_dir.mkdir(parents=True, exist_ok=True)
    test_file = out_dir / f"test_{site.function}_guard.py"
    cases_file = out_dir / f"{site.function}_cases.jsonl"
    created = []
    if not test_file.exists():
        params = _params(root, site)
        example = ", ".join(f'"{p}": ...' for p in params) or ""
        test_file.write_text(TEMPLATE.format(function=site.function, site_id=site.id, cases_name=cases_file.name,
                                             module=module_name(site.file), input_example=example),
                             encoding="utf-8")
        created.append(test_file)
    if not cases_file.exists():
        cases_file.write_text("", encoding="utf-8")
        created.append(cases_file)
    return Scaffold(site, test_file, cases_file, created)
