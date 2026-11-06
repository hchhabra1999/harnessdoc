"""Parse usage log lines from the formats people already have into one normalised record type.

Supported, auto-detected per line:
  * harnessdoc native records (written by ``harnessdoc.log.UsageLogger``)
  * raw provider responses: Anthropic Messages, OpenAI Chat Completions and Responses, Gemini
    (optionally wrapped: {"ts": ..., "task_id": ..., "site": ..., "response": {...}})
  * OpenTelemetry GenAI spans (flat ``attributes`` dicts or OTLP JSON key/value lists)
  * outcome records: {"type": "outcome", "task_id": ..., "success": true}

Provider token semantics differ (see pricing.Usage); every parser returns *split* counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..pricing import Usage

TASK_KEYS = ("task_id", "conversation_id", "session_id", "thread_id", "request_group")
SITE_KEYS = ("site", "call_site", "function", "tag", "route")


@dataclass
class UsageRecord:
    provider: str  # anthropic | openai | gemini | unknown
    model: str
    usage: Usage
    ts: datetime | None = None
    task_id: str | None = None
    site: str | None = None
    success: bool | None = None  # when the request record itself carries the outcome
    source_format: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    task_id: str
    success: bool
    ts: datetime | None = None


class ParseError(ValueError):
    pass


# ----------------------------------------------------------------------------- helpers


def _int(v: Any) -> int:
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int | float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    if isinstance(v, dict):  # OTLP AnyValue
        for k in ("intValue", "doubleValue", "stringValue"):
            if k in v:
                return _int(v[k])
    return 0


def _ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, int | float):
        # seconds, ms or ns since epoch
        x = float(v)
        if x > 1e17:
            x /= 1e9
        elif x > 1e12:
            x /= 1e3
        return datetime.fromtimestamp(x, tz=timezone.utc)
    if isinstance(v, str):
        if v.isdigit():
            return _ts(int(v))
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "success", "succeeded", "pass", "passed", "resolved", "ok"):
            return True
        if s in ("false", "no", "0", "failure", "failed", "fail", "error", "unresolved", "escalated"):
            return False
    return None


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    meta = d.get("metadata")
    if isinstance(meta, dict):
        for k in keys:
            if meta.get(k) not in (None, ""):
                return meta[k]
    return None


def _provider_from_model(model: str) -> str:
    m = model.lower()
    if "claude" in m:
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")) or "/gpt" in m:
        return "openai"
    if "gemini" in m:
        return "gemini"
    return "unknown"


# ----------------------------------------------------------------------------- provider responses


def _from_anthropic(resp: dict) -> tuple[str, Usage]:
    u = resp["usage"]
    ttl = None
    cc = u.get("cache_creation") or {}
    if _int(cc.get("ephemeral_1h_input_tokens")) and not _int(cc.get("ephemeral_5m_input_tokens")):
        ttl = "1h"
    return resp.get("model", ""), Usage.from_anthropic(
        _int(u.get("input_tokens")), _int(u.get("output_tokens")),
        _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens")),
        cache_write_ttl=ttl,
    )


def _from_openai_chat(resp: dict) -> tuple[str, Usage]:
    u = resp["usage"]
    details = u.get("prompt_tokens_details") or {}
    return resp.get("model", ""), Usage.from_openai(
        _int(u.get("prompt_tokens")), _int(u.get("completion_tokens")),
        _int(details.get("cached_tokens")), _int(details.get("cache_write_tokens")),
    )


def _from_openai_responses(resp: dict) -> tuple[str, Usage]:
    u = resp["usage"]
    details = u.get("input_tokens_details") or {}
    ttl = None
    wd = details.get("cache_write_tokens_details") or {}
    if _int(wd.get("ttl_12h_tokens")) and not _int(wd.get("ttl_30m_tokens")):
        ttl = "12h"
    return resp.get("model", ""), Usage.from_openai(
        _int(u.get("input_tokens")), _int(u.get("output_tokens")),
        _int(details.get("cached_tokens")), _int(details.get("cache_write_tokens")),
        cache_write_ttl=ttl,
    )


def _from_gemini(resp: dict) -> tuple[str, Usage]:
    u = resp.get("usage_metadata") or resp.get("usageMetadata") or {}

    def g(snake: str, camel: str) -> int:
        return _int(u.get(snake, u.get(camel)))

    model = resp.get("model_version") or resp.get("modelVersion") or resp.get("model") or ""
    return model, Usage.from_gemini(
        g("prompt_token_count", "promptTokenCount"), g("candidates_token_count", "candidatesTokenCount"),
        g("cached_content_token_count", "cachedContentTokenCount"), g("thoughts_token_count", "thoughtsTokenCount"),
    )


def parse_response(resp: dict) -> tuple[str, str, Usage, str] | None:
    """Detect a raw provider response. Returns (provider, model, usage, format) or None."""
    usage = resp.get("usage")
    if isinstance(usage, dict):
        if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage \
                or resp.get("type") == "message" or str(resp.get("id", "")).startswith("msg_"):
            model, u = _from_anthropic(resp)
            return "anthropic", model, u, "anthropic.message"
        if "prompt_tokens" in usage:
            # OpenAI chat shape. LiteLLM returns this shape for every provider, so take the provider
            # from the model name and fall back to openai.
            model, u = _from_openai_chat(resp)
            provider = _provider_from_model(model)
            return (provider if provider != "unknown" else "openai"), model, u, "openai.chat_completion"
        if "input_tokens" in usage and ("input_tokens_details" in usage or resp.get("object") == "response"):
            model, u = _from_openai_responses(resp)
            return "openai", model, u, "openai.response"
        if "input_tokens" in usage and "output_tokens" in usage:  # bare Anthropic-shaped usage
            model, u = _from_anthropic(resp)
            provider = _provider_from_model(model)
            return (provider if provider != "unknown" else "anthropic"), model, u, "anthropic.message"
    if isinstance(resp.get("usage_metadata"), dict) or isinstance(resp.get("usageMetadata"), dict):
        model, u = _from_gemini(resp)
        return "gemini", model, u, "gemini.generate_content"
    return None


# ----------------------------------------------------------------------------- OpenTelemetry GenAI


def _otel_attrs(span: dict) -> dict[str, Any]:
    attrs = span.get("attributes")
    if isinstance(attrs, list):  # OTLP JSON: [{"key": ..., "value": {"stringValue": ...}}]
        flat = {}
        for kv in attrs:
            if isinstance(kv, dict) and "key" in kv:
                v = kv.get("value", {})
                flat[kv["key"]] = next(iter(v.values())) if isinstance(v, dict) and v else v
        return flat
    return attrs if isinstance(attrs, dict) else {}


def parse_otel_span(span: dict) -> UsageRecord | None:
    a = _otel_attrs(span)
    if "gen_ai.usage.input_tokens" not in a and "gen_ai.usage.output_tokens" not in a:
        return None
    model = str(a.get("gen_ai.response.model") or a.get("gen_ai.request.model") or "")
    provider = str(a.get("gen_ai.provider.name") or a.get("gen_ai.system") or _provider_from_model(model))
    provider = {"gcp.gemini": "gemini", "gcp.vertex_ai": "gemini", "google": "gemini"}.get(provider, provider)
    cached = _int(a.get("gen_ai.usage.cache_read.input_tokens", a.get("gen_ai.usage.cache_read_input_tokens")))
    written = _int(a.get("gen_ai.usage.cache_creation.input_tokens",
                         a.get("gen_ai.usage.cache_creation_input_tokens")))
    # Assumption (GenAI semconv): gen_ai.usage.input_tokens includes cached tokens.
    usage = Usage.from_openai(_int(a.get("gen_ai.usage.input_tokens")), _int(a.get("gen_ai.usage.output_tokens")),
                              cached, written)
    site = a.get("code.function.name") or a.get("code.function")
    if a.get("code.filepath") and a.get("code.lineno"):
        site = f"{a['code.filepath']}:{a['code.lineno']}"
    return UsageRecord(
        provider=provider, model=model, usage=usage,
        ts=_ts(span.get("startTimeUnixNano") or span.get("start_time") or span.get("startTime")),
        task_id=str(a.get("harnessdoc.task_id") or a.get("session.id") or a.get("gen_ai.conversation.id")
                    or span.get("traceId") or span.get("trace_id") or "") or None,
        site=str(site) if site else None,
        source_format="otel.genai",
    )


# ----------------------------------------------------------------------------- entry point


def parse_line(obj: Any, task_key: str | None = None, site_key: str | None = None) -> UsageRecord | Outcome | None:
    """Parse one JSON object. Returns None for lines that carry no usage (e.g. non-LLM spans)."""
    if not isinstance(obj, dict):
        return None
    task_keys = (task_key,) if task_key else TASK_KEYS
    site_keys = (site_key,) if site_key else SITE_KEYS

    if obj.get("type") == "outcome" or ("success" in obj and "usage" not in obj and "response" not in obj
                                       and _first(obj, task_keys) is not None and "model" not in obj):
        tid = _first(obj, task_keys)
        ok = _bool(obj.get("success", obj.get("outcome")))
        if tid is None or ok is None:
            raise ParseError("outcome record needs a task id and a success value")
        return Outcome(str(tid), ok, _ts(obj.get("ts") or obj.get("timestamp")))

    # native harnessdoc record
    if obj.get("harnessdoc") and isinstance(obj.get("usage"), dict) and "uncached_input" in obj["usage"]:
        u = obj["usage"]
        return UsageRecord(
            provider=obj.get("provider") or _provider_from_model(obj.get("model", "")),
            model=obj.get("model", ""),
            usage=Usage(_int(u.get("uncached_input")), _int(u.get("output")), _int(u.get("cache_read")),
                        _int(u.get("cache_write")), u.get("cache_write_ttl"), bool(obj.get("batch"))),
            ts=_ts(obj.get("ts")), task_id=_s(_first(obj, task_keys)), site=_s(_first(obj, site_keys)),
            success=_bool(obj.get("success")), source_format="harnessdoc", meta=obj.get("meta") or {},
        )

    # OTel spans
    if "attributes" in obj and ("spanId" in obj or "span_id" in obj or "traceId" in obj or "name" in obj):
        rec = parse_otel_span(obj)
        if rec:
            return rec

    # raw or wrapped provider response
    inner = obj.get("response") if isinstance(obj.get("response"), dict) else obj
    parsed = parse_response(inner)
    if parsed is None:
        return None
    provider, model, usage, fmt = parsed
    if obj.get("batch") or inner.get("service_tier") == "batch":
        usage = Usage(usage.uncached_input, usage.output, usage.cache_read, usage.cache_write,
                      usage.cache_write_ttl, True)
    return UsageRecord(
        provider=provider, model=model or obj.get("model", ""), usage=usage,
        ts=_ts(obj.get("ts") or obj.get("timestamp") or inner.get("created") or inner.get("created_at")),
        task_id=_s(_first(obj, task_keys)), site=_s(_first(obj, site_keys)), success=_bool(obj.get("success")),
        source_format=fmt,
    )


def _s(v: Any) -> str | None:
    return None if v is None else str(v)
