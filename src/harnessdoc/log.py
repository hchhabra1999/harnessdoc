"""Tiny, dependency-free usage logger to drop into an AI app.

    from harnessdoc.log import UsageLogger
    usage_log = UsageLogger("logs/llm_usage.jsonl")

    response = client.messages.create(...)
    usage_log.record(response, task_id=ticket.id)           # site defaults to the calling function
    ...
    usage_log.outcome(ticket.id, success=ticket.resolved)   # once the task is over

Each line is a self-describing JSON record that `harnessdoc measure` reads. Only token counts, model,
timestamps and the ids you pass are written — never prompts or completions.
"""

from __future__ import annotations

import inspect
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .usage.records import parse_response


def _to_dict(response: Any) -> dict:
    if isinstance(response, dict):
        return response
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                continue
    raise TypeError(f"can't read usage from a {type(response).__name__}; pass the provider response object")


class UsageLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _write(self, rec: dict) -> None:
        line = json.dumps(rec, sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def record(self, response: Any, task_id: str | None = None, site: str | None = None,
               batch: bool = False, **meta: Any) -> None:
        """Log the usage of one provider response (Anthropic, OpenAI chat/responses, Gemini)."""
        data = _to_dict(response)
        parsed = parse_response(data)
        if parsed is None:
            raise ValueError("response has no recognisable usage block")
        provider, model, usage, fmt = parsed
        if site is None:
            caller = inspect.stack()[1]
            site = caller.function if caller.function != "<module>" else f"{Path(caller.filename).name}"
        self._write({
            "harnessdoc": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "usage": {
                "uncached_input": usage.uncached_input,
                "output": usage.output,
                "cache_read": usage.cache_read,
                "cache_write": usage.cache_write,
                "cache_write_ttl": usage.cache_write_ttl,
            },
            "batch": batch,
            "task_id": task_id,
            "site": site,
            "source_format": fmt,
            "meta": meta or None,
        })

    def outcome(self, task_id: str, success: bool, **meta: Any) -> None:
        """Mark a task as succeeded or failed — this is what 'cost per successful task' divides by."""
        self._write({"type": "outcome", "ts": datetime.now(timezone.utc).isoformat(),
                     "task_id": task_id, "success": bool(success), "meta": meta or None})
