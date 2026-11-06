"""Project goals — what the app is for and what "a successful task" means — kept in harnessdoc.goals.yaml.

The goals file is written once (by the /harnessdoc skill's interview, `harnessdoc goals init`, or by hand)
and reused on every run, so reports compare against the same definition of success each time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

GOALS_FILE = "harnessdoc.goals.yaml"
PRIORITIES = ("cost", "quality", "latency")


class GoalsError(ValueError):
    pass


@dataclass
class Goals:
    app: str  # what the app does, one sentence
    success: str = ""  # what counts as a successful task, in the user's words
    task_key: str | None = None  # log field that groups requests into tasks
    monthly_tasks: int | None = None
    quality_bar: str = ""  # e.g. "success rate must not drop by more than 1 point"
    max_success_rate_drop: float = 1.0  # percentage points a change may cost before it's rejected
    target_cost_per_success: float | None = None  # USD
    priorities: list[str] = field(default_factory=lambda: ["cost", "quality"])
    constraints: list[str] = field(default_factory=list)  # e.g. "agent must stay on Anthropic"
    logs: list[str] = field(default_factory=list)  # paths relative to the project root
    outcomes: str | None = None
    notes: str = ""

    def validate(self) -> Goals:
        problems = []
        if not self.app or not self.app.strip():
            problems.append("`app` is required: one sentence on what the app does")
        if self.monthly_tasks is not None and self.monthly_tasks < 0:
            problems.append("`monthly_tasks` can't be negative")
        if self.target_cost_per_success is not None and self.target_cost_per_success <= 0:
            problems.append("`target_cost_per_success` must be a positive dollar amount")
        if not 0 <= self.max_success_rate_drop <= 100:
            problems.append("`max_success_rate_drop` is in percentage points (0–100)")
        bad = [p for p in self.priorities if p not in PRIORITIES]
        if bad:
            problems.append(f"unknown priorities {bad}; use any of {list(PRIORITIES)}")
        if problems:
            raise GoalsError("; ".join(problems))
        return self

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goals:
        if not isinstance(data, dict):
            raise GoalsError("goals must be a mapping")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise GoalsError(f"unknown goals fields: {unknown}")
        data = dict(data)
        for key in ("priorities", "constraints", "logs"):
            if isinstance(data.get(key), str):
                data[key] = [data[key]]
        if "app" not in data:
            raise GoalsError("`app` is required: one sentence on what the app does")
        return cls(**data).validate()


def goals_path(root: str | Path) -> Path:
    p = Path(root)
    return p if p.suffix in (".yaml", ".yml") else p / GOALS_FILE


def load_goals(root: str | Path) -> Goals | None:
    path = goals_path(root)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise GoalsError(f"{path.name} is not valid YAML: {e}") from e
    return Goals.from_dict(data)


HEADER = """\
# harnessdoc goals — what this app is for and what counts as success.
# Read by `harnessdoc report` and the /harnessdoc skill on every run. Edit freely.
"""


def save_goals(goals: Goals, root: str | Path) -> Path:
    goals.validate()
    path = goals_path(root)
    body = yaml.safe_dump(goals.as_dict(), sort_keys=False, allow_unicode=True, width=100)
    path.write_text(HEADER + body, encoding="utf-8")
    return path
