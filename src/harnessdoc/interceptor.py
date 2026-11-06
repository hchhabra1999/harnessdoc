"""Intercept provider SDK calls during an experiment: apply a variant's changes and record token usage.

Only used by `harnessdoc experiment run`. Patches are installed on entering the context manager and always
removed on exit (including on errors). harnessdoc never reads API keys — the SDKs read their own
environment — and never stores prompts or completions.
"""

from __future__ import annotations

import contextvars
import copy
import functools
import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .pricing import Usage
from .usage.records import parse_response


@dataclass(frozen=True)
class Target:
    module: str
    cls: str
    method: str
    provider: str
    api: str  # chat | messages | responses | gemini


# Verified against anthropic 2.6, openai 3.46 and google-genai 3.0.
TARGETS: tuple[Target, ...] = (
    Target("anthropic.resources.messages.messages", "Messages", "create", "anthropic", "messages"),
    Target("anthropic.resources.messages.messages", "AsyncMessages", "create", "anthropic", "messages"),
    Target("anthropic.resources.beta.messages.messages", "Messages", "create", "anthropic", "messages"),
    Target("anthropic.resources.beta.messages.messages", "AsyncMessages", "create", "anthropic", "messages"),
    Target("openai.resources.chat.completions.completions", "Completions", "create", "openai", "chat"),
    Target("openai.resources.chat.completions.completions", "AsyncCompletions", "create", "openai", "chat"),
    Target("openai.resources.responses.responses", "Responses", "create", "openai", "responses"),
    Target("openai.resources.responses.responses", "AsyncResponses", "create", "openai", "responses"),
    Target("google.genai.models", "Models", "generate_content", "gemini", "gemini"),
    Target("google.genai.models", "AsyncModels", "generate_content", "gemini", "gemini"),
)

_INSIDE: contextvars.ContextVar[bool] = contextvars.ContextVar("harnessdoc_inside_intercepted_call",
                                                               default=False)

MAX_TOKENS_KEY = {"messages": "max_tokens", "chat": "max_completion_tokens", "responses": "max_output_tokens"}


class BudgetExceeded(BaseException):
    """Raised before an SDK call once the experiment budget is spent.

    A BaseException (like KeyboardInterrupt) so application code with a broad `except Exception: continue`
    retry loop can't swallow it and keep spending.
    """


@dataclass
class Variant:
    name: str
    model_overrides: dict[str, str] = field(default_factory=dict)  # {"from-model": "to-model"} or {"*": "model"}
    max_tokens: int | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)  # extra top-level kwargs, e.g. cache_control
    env: dict[str, str] = field(default_factory=dict)  # environment variables set while this variant runs

    def apply(self, target: Target, kwargs: dict[str, Any]) -> dict[str, Any]:
        out = dict(kwargs)
        model = out.get("model")
        if isinstance(model, str):
            new = self.model_overrides.get(model, self.model_overrides.get("*"))
            if new:
                out["model"] = new
        if self.max_tokens is not None:
            if target.api == "chat" and "max_tokens" in out and "max_completion_tokens" not in out:
                out["max_tokens"] = self.max_tokens
            elif target.api in MAX_TOKENS_KEY:
                out[MAX_TOKENS_KEY[target.api]] = self.max_tokens
            # gemini: max_output_tokens lives inside `config`; not overridden here
        for k, v in self.kwargs.items():
            out[k] = copy.deepcopy(v)
        return out


@dataclass
class Call:
    provider: str
    model: str
    usage: Usage | None  # None when the response carried no usage (e.g. a stream)


class Interceptor:
    """Context manager that patches every importable target for the duration of a run."""

    def __init__(self, variant: Variant, on_call: Callable[[Call], None],
                 before_call: Callable[[], None] | None = None, targets: tuple[Target, ...] = TARGETS):
        self.variant = variant
        self.on_call = on_call
        self.before_call = before_call or (lambda: None)
        self.targets = targets
        self._saved: list[tuple[type, str, Any]] = []

    def __enter__(self) -> Interceptor:  # noqa: PYI034 — typing.Self needs Python 3.11; we support 3.10
        for t in self.targets:
            try:
                cls = getattr(importlib.import_module(t.module), t.cls)
            except (ImportError, AttributeError):
                continue  # SDK not installed (or a different version): nothing to patch
            original = cls.__dict__.get(t.method)
            if original is None:
                continue
            self._saved.append((cls, t.method, original))
            setattr(cls, t.method, self._wrap(t, original))
        return self

    def __exit__(self, *exc) -> None:
        for cls, name, original in reversed(self._saved):
            setattr(cls, name, original)
        self._saved.clear()

    @property
    def patched(self) -> int:
        return len(self._saved)

    def _record(self, t: Target, kwargs: dict, response: Any) -> None:
        data = response if isinstance(response, dict) else _dump(response)
        parsed = parse_response(data) if data else None
        model = str(kwargs.get("model") or (parsed[1] if parsed else ""))
        self.on_call(Call(t.provider, model, parsed[2] if parsed else None))

    def _wrap(self, t: Target, original: Callable) -> Callable:
        # Only the outermost intercepted call applies the variant and records usage; an SDK method that
        # delegates to another patched method must not be counted twice.
        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def async_wrapper(self_, *args, **kwargs):
                if _INSIDE.get():
                    return await original(self_, *args, **kwargs)
                self.before_call()
                kwargs = self.variant.apply(t, kwargs)
                token = _INSIDE.set(True)
                try:
                    response = await original(self_, *args, **kwargs)
                finally:
                    _INSIDE.reset(token)
                self._record(t, kwargs, response)
                return response

            return async_wrapper

        @functools.wraps(original)
        def wrapper(self_, *args, **kwargs):
            if _INSIDE.get():
                return original(self_, *args, **kwargs)
            self.before_call()
            kwargs = self.variant.apply(t, kwargs)
            token = _INSIDE.set(True)
            try:
                response = original(self_, *args, **kwargs)
            finally:
                _INSIDE.reset(token)
            if inspect.isawaitable(response):
                # Async SDK methods behind sync-looking decorators (anthropic AsyncMessages, openai
                # AsyncCompletions) return a coroutine: record once it resolves.
                async def finish(awaitable=response, call_kwargs=kwargs):
                    inner = _INSIDE.set(True)
                    try:
                        result = await awaitable
                    finally:
                        _INSIDE.reset(inner)
                    self._record(t, call_kwargs, result)
                    return result

                return finish()
            self._record(t, kwargs, response)
            return response

        return wrapper


def _dump(response: Any) -> dict | None:
    for attr in ("model_dump", "to_dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                out = fn()
                return out if isinstance(out, dict) else None
            except TypeError:
                continue
    return None
