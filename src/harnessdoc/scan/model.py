"""Data types produced by the scanner. Everything is plain data so reports are deterministic."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Resolved:
    """A value as far as static analysis could resolve it."""

    kind: str  # "literal" | "file" | "fstring" | "call" | "dynamic" | "absent"
    value: Any = None  # literal value / file path / function name
    tokens: int | None = None  # rough token estimate when the text is known
    volatile: list[str] = field(default_factory=list)  # e.g. ["datetime.now"] found inside
    volatile_line: int | None = None  # where the first volatile value is built
    leading_volatile: bool = False  # the volatile value comes before static text
    nondeterministic_line: int | None = None  # e.g. iteration over a set when building tools
    source_file: str | None = None  # file where the value is built, when not the call's own file


@dataclass
class CallSite:
    id: str  # "<file>:<line>"
    file: str
    line: int
    provider: str  # anthropic | openai | gemini | litellm
    api: str  # e.g. "messages.create", "chat.completions.create"
    function: str | None  # enclosing function name
    model: Resolved
    max_tokens: int | None
    system: Resolved
    tools: Resolved
    prompt_text: str  # all string literals in the call + resolved prompts (lower-cased), for heuristics
    has_cache_control: bool
    streaming: bool
    batch_api: bool
    in_for_loop: int | None  # line of the innermost enclosing for-loop
    in_while_true: int | None  # line of the innermost enclosing `while True`
    retry_continue: int | None  # line of an `except ...: continue` wrapping the call inside that loop
    loop_has_counter: bool  # the while-True loop increments and compares a counter
    is_test_file: bool
    context_text: str  # lower-cased path + function name + docstrings, for job/batch hints
    kw_lines: dict[str, int] = field(default_factory=dict)  # keyword argument -> line

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    rule: str  # HD001...
    title: str
    category: str  # caching | loop | model | output | code | batch | coverage
    severity: str  # high | medium | low | info
    file: str
    line: int
    call_site: str | None
    evidence: str
    recommendation: str
    est_saving: str | None = None  # human-readable, always an estimate until measured
    est_saving_pct: float | None = None  # share of the affected call site's cost
    guard: list[str] = field(default_factory=list)  # evals that cover the affected code
    related_lines: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    root: str
    files_scanned: int
    call_sites: list[CallSite]
    findings: list[Finding]
    coverage: dict[str, list[str]]  # call-site id -> eval files that reference its function
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "call_sites": [c.as_dict() for c in self.call_sites],
            "findings": [f.as_dict() for f in self.findings],
            "coverage": self.coverage,
            "errors": self.errors,
        }
