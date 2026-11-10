"""Claude Code session logs → what your coding agent actually cost, and where the tokens went.

Claude Code writes every session to ``~/.claude/projects/<project>/<session>.jsonl`` and each subagent to
``<session>/subagents/agent-*.jsonl``. This module reads only token counts, model ids, timestamps, session ids
and the working directory — never prompts, replies or tool output — and prices them from the price book.

Three facts about the format that matter for correctness (checked against real logs):

* A message is written **once per content block**, each time with the same ``usage``: summing records naively
  over-counted tokens by 1.8–2.95× in the logs checked. Records are de-duplicated by message id.
* Cache writes carry a TTL split (``usage.cache_creation.ephemeral_5m/1h_input_tokens``). The 1-hour write costs
  2× input, the 5-minute write 1.25×, and 1-hour writes dominated in the logs checked, so the TTL is priced.
* Subagents are separate files. Skipping them under-counts, so the scan is recursive and tags them.

Costs are **API-equivalent at list prices**. A subscription plan does not bill per token, so read the dollars
as "what this usage would cost on the API", and the cache and model breakdowns as the actionable part.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .pricing import PriceBook, UnknownModel, Usage

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
SYNTHETIC = "<synthetic>"  # placeholder messages Claude Code writes itself (errors, interrupts): never billed


@dataclass
class Message:
    id: str
    session: str
    model: str
    ts: datetime | None
    cwd: str | None
    subagent: bool
    usage: Usage  # standard-rate parts: uncached + output + reads + 5m writes
    write_1h: int
    fast: bool
    total_tokens: int


@dataclass
class Parts:
    uncached_input: float = 0.0
    cache_read: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    output: float = 0.0

    @property
    def total(self) -> float:
        return self.uncached_input + self.cache_read + self.cache_write_5m + self.cache_write_1h + self.output

    def add(self, other: Parts) -> None:
        self.uncached_input += other.uncached_input
        self.cache_read += other.cache_read
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.output += other.output


@dataclass
class Group:
    key: str
    messages: int = 0
    cost: Parts = field(default_factory=Parts)
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    @property
    def input_total(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cache_read / self.input_total if self.input_total else None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "messages": self.messages, "cost": self.cost.total,
                "cost_parts": vars(self.cost), "tokens": {"uncached_input": self.input_uncached,
                                                          "cache_read": self.cache_read,
                                                          "cache_write": self.cache_write, "output": self.output},
                "cache_hit_rate": self.cache_hit_rate}


@dataclass
class SessionSummary:
    session: str
    project: str | None
    start: datetime | None
    end: datetime | None
    messages: int
    models: list[str]
    cost: float
    subagent_cost: float


@dataclass
class Report:
    files: int
    subagent_files: int
    records_without_usage: int
    unreadable_lines: int
    duplicate_records: int  # extra copies of an already-counted message id
    messages: int
    window: tuple[datetime, datetime] | None
    total: Group
    by_model: dict[str, Group]
    by_day: dict[str, Group]
    by_project: dict[str, Group]
    subagent: Group
    sessions: list[SessionSummary]
    unpriced_models: dict[str, int]
    fast_messages: int
    fast_unpriced: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files, "subagent_files": self.subagent_files, "messages": self.messages,
            "duplicate_records": self.duplicate_records, "unreadable_lines": self.unreadable_lines,
            "window": [d.isoformat() for d in self.window] if self.window else None,
            "total": self.total.as_dict(),
            "by_model": {k: g.as_dict() for k, g in sorted(self.by_model.items())},
            "by_day": {k: g.as_dict() for k, g in sorted(self.by_day.items())},
            "by_project": {k: g.as_dict() for k, g in sorted(self.by_project.items())},
            "subagent": self.subagent.as_dict(),
            "sessions": [{**vars(s), "start": s.start.isoformat() if s.start else None,
                          "end": s.end.isoformat() if s.end else None} for s in self.sessions],
            "unpriced_models": self.unpriced_models,
            "fast_messages": self.fast_messages, "fast_unpriced": self.fast_unpriced,
        }


# ----------------------------------------------------------------------------- reading


def _ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0


def parse_message(rec: dict[str, Any], subagent: bool) -> Message | None:
    """One log record → Message, or None when it carries no billable usage."""
    msg = rec.get("message")
    if rec.get("type") != "assistant" or not isinstance(msg, dict):
        return None
    u = msg.get("usage")
    model = msg.get("model")
    if not isinstance(u, dict) or not model or model == SYNTHETIC or not msg.get("id"):
        return None
    write_total = _int(u.get("cache_creation_input_tokens"))
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    w1h = min(_int(cc.get("ephemeral_1h_input_tokens")), write_total)
    parsed = Usage(_int(u.get("input_tokens")), _int(u.get("output_tokens")), _int(u.get("cache_read_input_tokens")),
                   write_total - w1h, "5m")
    total = parsed.uncached_input + parsed.output + parsed.cache_read + write_total
    if total == 0:
        return None
    return Message(str(msg["id"]), str(rec.get("sessionId") or rec.get("session_id") or ""), str(model),
                   _ts(rec.get("timestamp")), rec.get("cwd"), subagent, parsed, w1h,
                   u.get("speed") == "fast", total)


def read_messages(root: Path, since: datetime | None = None, until: datetime | None = None,
                  cwd_prefix: str | None = None) -> tuple[list[Message], dict[str, int]]:
    """All distinct billable messages under `root`, plus counters about what was skipped."""
    stats = {"files": 0, "subagent_files": 0, "no_usage": 0, "unreadable": 0, "duplicates": 0}
    best: dict[str, Message] = {}
    for path in sorted(root.rglob("*.jsonl")):
        sub = "subagents" in path.parts
        stats["files"] += 1
        stats["subagent_files"] += sub
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            stats["unreadable"] += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["unreadable"] += 1
                continue
            m = parse_message(rec, sub) if isinstance(rec, dict) else None
            if m is None:
                if isinstance(rec, dict) and rec.get("type") == "assistant":
                    stats["no_usage"] += 1
                continue
            old = best.get(m.id)
            if old is not None:
                stats["duplicates"] += 1
                if m.total_tokens <= old.total_tokens:  # same usage on every copy; keep the fullest defensively
                    continue
            best[m.id] = m
    msgs = []
    for m in best.values():
        if since and (m.ts is None or m.ts < since):
            continue
        if until and (m.ts is None or m.ts >= until):
            continue
        if cwd_prefix and not (m.cwd or "").startswith(cwd_prefix):
            continue
        msgs.append(m)
    msgs.sort(key=lambda m: (m.ts or datetime.min.replace(tzinfo=timezone.utc), m.id))
    return msgs, stats


# ----------------------------------------------------------------------------- pricing + aggregation


def price_message(m: Message, book: PriceBook) -> tuple[Parts, bool]:
    """Cost of one message by component. The bool says a fast-mode message could only be priced at the
    standard rate (no fast-mode price on file for that model)."""
    on = m.ts.date() if m.ts else None
    standard = book.cost(m.model, m.usage, on=on)  # m.usage holds the 5-minute writes only
    parts = Parts(standard.uncached_input, standard.cache_read, standard.cache_write, 0.0, standard.output)
    if m.write_1h:
        parts.cache_write_1h = book.cost(m.model, Usage(0, 0, 0, m.write_1h, "1h"), on=on).cache_write
    unpriced_fast = False
    if m.fast:
        fast = book.price(m.model).raw.get("fast_mode")
        base = book.price(m.model)
        if fast and base.input:
            k = fast["input"] / base.input
            for name in ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h", "output"):
                setattr(parts, name, getattr(parts, name) * k)
        else:
            unpriced_fast = True
    return parts, unpriced_fast


def _add(g: Group, m: Message, parts: Parts) -> None:
    g.messages += 1
    g.cost.add(parts)
    g.input_uncached += m.usage.uncached_input
    g.cache_read += m.usage.cache_read
    g.cache_write += m.usage.cache_write + m.write_1h
    g.output += m.usage.output


def analyse(root: Path | str = DEFAULT_ROOT, book: PriceBook | None = None, since: datetime | None = None,
            until: datetime | None = None, cwd_prefix: str | None = None, top: int = 10) -> Report:
    root = Path(root).expanduser()
    book = book or PriceBook.load()
    msgs, stats = read_messages(root, since, until, cwd_prefix)

    total, subagent = Group("total"), Group("subagents")
    by_model: dict[str, Group] = {}
    by_day: dict[str, Group] = {}
    by_project: dict[str, Group] = {}
    sessions: dict[str, dict[str, Any]] = {}
    unpriced: dict[str, int] = defaultdict(int)
    fast_msgs = fast_unpriced = 0

    for m in msgs:
        try:
            parts, fast_flag = price_message(m, book)
        except UnknownModel:
            unpriced[m.model] += 1
            parts, fast_flag = Parts(), False
        fast_msgs += m.fast
        fast_unpriced += fast_flag
        model_key = book.resolve_id(m.model) or m.model
        day = m.ts.date().isoformat() if m.ts else "unknown"
        for g in (total, by_model.setdefault(model_key, Group(model_key)), by_day.setdefault(day, Group(day)),
                  by_project.setdefault(m.cwd or "(unknown)", Group(m.cwd or "(unknown)"))):
            _add(g, m, parts)
        if m.subagent:
            _add(subagent, m, parts)
        s = sessions.setdefault(m.session, {"project": m.cwd, "start": m.ts, "end": m.ts, "n": 0, "models": set(),
                                            "cost": 0.0, "sub": 0.0})
        s["n"] += 1
        s["models"].add(model_key)
        s["cost"] += parts.total
        s["sub"] += parts.total if m.subagent else 0.0
        if m.ts and (s["start"] is None or m.ts < s["start"]):
            s["start"] = m.ts
        if m.ts and (s["end"] is None or m.ts > s["end"]):
            s["end"] = m.ts

    ranked = sorted(sessions.items(), key=lambda kv: -kv[1]["cost"])[:top]
    stamps = [m.ts for m in msgs if m.ts]
    return Report(
        files=stats["files"], subagent_files=stats["subagent_files"], records_without_usage=stats["no_usage"],
        unreadable_lines=stats["unreadable"], duplicate_records=stats["duplicates"], messages=len(msgs),
        window=(min(stamps), max(stamps)) if stamps else None, total=total, by_model=by_model, by_day=by_day,
        by_project=by_project, subagent=subagent,
        sessions=[SessionSummary(k, v["project"], v["start"], v["end"], v["n"], sorted(v["models"]), v["cost"],
                                 v["sub"]) for k, v in ranked],
        unpriced_models=dict(unpriced), fast_messages=fast_msgs, fast_unpriced=fast_unpriced)


def parse_since(value: str) -> datetime:
    """'7d' → seven days ago; '2026-11-01' → that day, UTC."""
    v = value.strip().lower()
    if v.endswith("d") and v[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(days=int(v[:-1]))
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=timezone.utc)
