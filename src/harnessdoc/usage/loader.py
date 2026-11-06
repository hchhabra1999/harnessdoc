"""Read usage logs (JSONL / JSON / OTLP JSON exports, optionally gzipped) into records and outcomes."""

from __future__ import annotations

import csv
import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .records import Outcome, ParseError, UsageRecord, _bool, parse_line


@dataclass
class LoadResult:
    records: list[UsageRecord] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    lines_without_usage: int = 0
    errors: list[str] = field(default_factory=list)  # capped sample of "file:line: reason"
    error_count: int = 0
    files: list[str] = field(default_factory=list)


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def _expand(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.rglob("*")
                          if f.is_file() and any(s in (".jsonl", ".json", ".ndjson") for s in f.suffixes))
        else:
            out.append(p)
    return out


def _objects(path: Path) -> Iterator[tuple[int, Any]]:
    """A whole-file JSON document (array, single object or OTLP export), else JSON Lines."""
    with _open(path) as fh:
        text = fh.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        yield from enumerate(data, 1)
        return
    if isinstance(data, dict):
        yield from enumerate(_flatten_otlp(data), 1)
        return
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield i, json.loads(line)
        except json.JSONDecodeError as e:  # one truncated line must not drop the rest of the file
            yield i, e


def _flatten_otlp(data: dict) -> list[Any]:
    """OTLP JSON export ({"resourceSpans": [{"scopeSpans": [{"spans": [...]}]}]}) -> list of spans."""
    if "resourceSpans" not in data:
        return [data]
    spans = []
    for rs in data.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []):
            spans += ss.get("spans", [])
    return spans


def load_logs(paths: list[Path], task_key: str | None = None, site_key: str | None = None,
              max_error_samples: int = 10) -> LoadResult:
    res = LoadResult()
    for path in _expand(paths):
        res.files.append(str(path))
        try:
            objects = list(_objects(path))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            res.error_count += 1
            if len(res.errors) < max_error_samples:
                res.errors.append(f"{path}: {e.__class__.__name__}: {e}")
            continue
        for lineno, obj in objects:
            if isinstance(obj, json.JSONDecodeError):
                res.error_count += 1
                if len(res.errors) < max_error_samples:
                    res.errors.append(f"{path.name}:{lineno}: invalid JSON ({obj.msg})")
                continue
            try:
                parsed = parse_line(obj, task_key, site_key)
            except (ParseError, KeyError, TypeError, ValueError) as e:
                res.error_count += 1
                if len(res.errors) < max_error_samples:
                    res.errors.append(f"{path.name}:{lineno}: {e}")
                continue
            if isinstance(parsed, UsageRecord):
                res.records.append(parsed)
            elif isinstance(parsed, Outcome):
                res.outcomes.append(parsed)
            else:
                res.lines_without_usage += 1
    return res


def load_outcomes_file(path: Path, task_key: str = "task_id") -> list[Outcome]:
    """Outcomes from a CSV (task_id,success) or JSONL file of outcome records."""
    if path.suffix.lower() == ".csv":
        out = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ok = _bool(row.get("success", row.get("outcome")))
                if row.get(task_key) and ok is not None:
                    out.append(Outcome(row[task_key], ok))
        return out
    return [o for o in load_logs([path], task_key).outcomes]
