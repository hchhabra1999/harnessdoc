import gzip
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.log import UsageLogger
from harnessdoc.measure import MeasuredFinding, combined_saving, match_site
from harnessdoc.pricing import PriceBook, Usage
from harnessdoc.scan.model import CallSite, Finding, Resolved
from harnessdoc.usage import load_logs, parse_line
from harnessdoc.usage.analyze import baseline, ratio_interval, wilson
from harnessdoc.usage.records import Outcome, UsageRecord

ROOT = Path(__file__).resolve().parent.parent
BOOK = PriceBook.load()


# ----------------------------------------------------------------------------- parsing

ANTHROPIC = {"id": "msg_1", "type": "message", "model": "claude-opus-5-5",
             "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900,
                       "cache_creation_input_tokens": 50}}
OPENAI_CHAT = {"object": "chat.completion", "model": "gpt-6.1-sol",
               "usage": {"prompt_tokens": 1000, "completion_tokens": 30,
                         "prompt_tokens_details": {"cached_tokens": 800}}}
OPENAI_RESP = {"object": "response", "model": "gpt-6.1-sol",
               "usage": {"input_tokens": 1000, "output_tokens": 40,
                         "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 300,
                                                  "cache_write_tokens_details": {"ttl_12h_tokens": 300}}}}
GEMINI_SNAKE = {"model_version": "gemini-2.5-flash",
                "usage_metadata": {"prompt_token_count": 500, "candidates_token_count": 20,
                                   "cached_content_token_count": 400, "thoughts_token_count": 10}}
GEMINI_CAMEL = {"modelVersion": "gemini-2.5-flash",
                "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 20}}


@pytest.mark.parametrize("obj,provider,split,fmt", [
    (ANTHROPIC, "anthropic", (100, 900, 50, 20), "anthropic.message"),
    (OPENAI_CHAT, "openai", (200, 800, 0, 30), "openai.chat_completion"),
    (OPENAI_RESP, "openai", (100, 600, 300, 40), "openai.response"),
    (GEMINI_SNAKE, "gemini", (100, 400, 0, 30), "gemini.generate_content"),
    (GEMINI_CAMEL, "gemini", (500, 0, 0, 20), "gemini.generate_content"),
])
def test_provider_responses(obj, provider, split, fmt):
    r = parse_line(obj)
    assert isinstance(r, UsageRecord)
    assert r.provider == provider and r.source_format == fmt
    u = r.usage
    assert (u.uncached_input, u.cache_read, u.cache_write, u.output) == split


def test_openai_responses_12h_ttl_detected():
    assert parse_line(OPENAI_RESP).usage.cache_write_ttl == "12h"


def test_litellm_style_openai_shape_for_claude_keeps_provider():
    obj = {"model": "claude-sonnet-5-5", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    assert parse_line(obj).provider == "anthropic"


def test_wrapped_response_carries_task_site_time():
    r = parse_line({"ts": "2026-11-01T10:00:00Z", "task_id": 7, "site": "respond", "response": ANTHROPIC})
    assert (r.task_id, r.site) == ("7", "respond")
    assert r.ts == datetime(2026, 11, 1, 10, tzinfo=timezone.utc)


def test_metadata_task_id_and_custom_keys():
    r = parse_line({"metadata": {"conversation_id": "c9"}, "response": ANTHROPIC})
    assert r.task_id == "c9"
    r = parse_line({"ticket": "T1", "route": "x", "response": ANTHROPIC}, task_key="ticket", site_key="route")
    assert (r.task_id, r.site) == ("T1", "x")


def test_outcome_records():
    o = parse_line({"type": "outcome", "task_id": "T1", "success": "resolved"})
    assert isinstance(o, Outcome) and o.success is True
    o = parse_line({"task_id": "T2", "success": False})
    assert isinstance(o, Outcome) and o.success is False


def test_otel_flat_and_otlp_attributes():
    flat = {"name": "chat", "traceId": "abc", "startTimeUnixNano": 1_762_000_000_000_000_000,
            "attributes": {"gen_ai.provider.name": "anthropic", "gen_ai.response.model": "claude-opus-5-5",
                           "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 50,
                           "gen_ai.usage.cache_read.input_tokens": 700, "code.function.name": "respond"}}
    r = parse_line(flat)
    assert (r.usage.uncached_input, r.usage.cache_read, r.usage.output) == (300, 700, 50)
    assert (r.task_id, r.site, r.source_format) == ("abc", "respond", "otel.genai")

    otlp = {"spanId": "s", "traceId": "t", "attributes": [
        {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
        {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-5.4"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "120"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "8"}},
    ]}
    r = parse_line(otlp)
    assert (r.provider, r.model, r.usage.uncached_input, r.usage.output) == ("openai", "gpt-5.4", 120, 8)


def test_lines_without_usage_are_ignored():
    assert parse_line({"event": "user_signup"}) is None
    assert parse_line({"name": "db.query", "spanId": "1", "attributes": {"db.system": "postgres"}}) is None
    assert parse_line([1, 2]) is None


# ----------------------------------------------------------------------------- loading


def test_loader_formats_and_errors(tmp_path):
    (tmp_path / "a.jsonl").write_text(json.dumps(ANTHROPIC) + "\n\n{bad json\n" + json.dumps(OPENAI_CHAT) + "\n")
    (tmp_path / "b.json").write_text(json.dumps([GEMINI_SNAKE, {"type": "outcome", "task_id": "x",
                                                                "success": True}]))
    with gzip.open(tmp_path / "c.jsonl.gz", "wt") as fh:
        fh.write(json.dumps(OPENAI_RESP) + "\n")
    (tmp_path / "otlp.json").write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {"spanId": "1", "traceId": "t", "attributes": {"gen_ai.request.model": "gpt-5.4",
                                                       "gen_ai.usage.input_tokens": 5,
                                                       "gen_ai.usage.output_tokens": 1}}]}]}]}))
    res = load_logs([tmp_path])
    # a.jsonl has a bad line -> the file is still not JSON as a whole, so it is read line by line
    assert len(res.records) == 4 + 1  # anthropic, openai chat, gemini, openai responses, otel
    assert len(res.outcomes) == 1
    assert res.error_count == 1 and "a.jsonl:3" in res.errors[0]


# ----------------------------------------------------------------------------- statistics


def test_wilson_and_ratio_basics():
    w = wilson(8, 10)
    assert w.value == 0.8 and w.low == pytest.approx(0.4902, abs=1e-4) and w.high == pytest.approx(0.9433, abs=1e-4)
    assert wilson(0, 0) is None
    assert ratio_interval([1.0, 2.0], [0, 0]) is None
    r = ratio_interval([1.0, 1.0, 1.0, 1.0], [1, 1, 1, 1])
    assert r.value == 1.0 and r.low == r.high == 1.0  # no variance -> zero-width interval


def _rec(model, inp, out, task=None, ts=None, site=None, success=None):
    return UsageRecord(provider="", model=model, usage=Usage(inp, out), task_id=task, site=site,
                       ts=ts, success=success)


def test_baseline_cost_per_success_counts_failed_task_spend():
    # Two tasks on Opus 5.5 ($4/$20): T1 succeeds, T2 fails. Each costs 1M input = $4.
    recs = [_rec("claude-opus-5-5", 1_000_000, 0, "T1"), _rec("claude-opus-5-5", 1_000_000, 0, "T2")]
    b = baseline(recs, [Outcome("T1", True), Outcome("T2", False)], BOOK)
    assert b.total.cost == pytest.approx(8.0)
    assert b.cost_per_success.value == pytest.approx(8.0)  # $8 spent for 1 success, not $4
    assert b.successes == 1 and b.tasks_with_outcome == 2


def test_baseline_unpriced_untagged_and_latest_outcome():
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 2, tzinfo=timezone.utc)
    recs = [_rec("claude-haiku-5-5", 1000, 10, "T1", t0, "a"), _rec("mystery-model", 10, 1, None, t1)]
    outs = [Outcome("T1", False, t0), Outcome("T1", True, t1)]  # later outcome wins
    b = baseline(recs, outs, BOOK)
    assert b.unpriced_models == {"mystery-model": 1}
    assert set(b.by_site) == {"a", "(untagged)"}
    assert b.successes == 1
    assert b.requests_without_outcome == 1
    assert b.window_days == pytest.approx(1.0) and b.monthly_factor == pytest.approx(30.0)


def test_monthly_projection_needs_a_day_of_data():
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    b = baseline([_rec("claude-haiku-5-5", 10, 1, ts=t), _rec("claude-haiku-5-5", 10, 1, ts=t)], [], BOOK)
    assert b.monthly_factor is None


def test_outcome_on_the_request_record():
    b = baseline([_rec("claude-haiku-5-5", 10, 1, "T1", success=True)], [], BOOK)
    assert b.successes == 1


# ----------------------------------------------------------------------------- logger


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


def test_usage_logger_roundtrip(tmp_path):
    log = UsageLogger(tmp_path / "logs" / "u.jsonl")

    def classify_intent():
        log.record(FakeResponse(ANTHROPIC), task_id="T1")  # site defaults to this function's name

    classify_intent()
    log.record(OPENAI_CHAT, task_id="T1", site="extract")
    log.outcome("T1", success=True)
    text = (tmp_path / "logs" / "u.jsonl").read_text()
    assert "content" not in text  # never writes prompts or completions
    res = load_logs([tmp_path / "logs" / "u.jsonl"])
    assert [r.site for r in res.records] == ["classify_intent", "extract"]
    assert res.records[0].usage == Usage(100, 20, 900, 50)
    b = baseline(res.records, res.outcomes, BOOK)
    assert b.successes == 1 and b.cost_per_success.value == pytest.approx(b.total.cost)
    with pytest.raises(ValueError):
        log.record({"no": "usage"})


# ----------------------------------------------------------------------------- join with scan


def _site(file, line, fn):
    r = Resolved("absent")
    return CallSite(f"{file}:{line}", file, line, "anthropic", "messages.create", fn, r, None, r, r, "", False,
                    False, False, None, None, None, False, False, "")


def test_match_site_labels():
    sites = [_site("app/bot.py", 10, "respond"), _site("app/router.py", 5, "classify")]
    assert match_site("app/bot.py:10", sites).function == "respond"
    assert match_site("respond", sites).function == "respond"
    assert match_site("app.router.classify", sites).function == "classify"
    assert match_site("/srv/app/router.py:5", sites).function == "classify"
    assert match_site("unknown", sites) is None


def test_combined_saving_does_not_double_count():
    f1 = Finding("HD006", "", "model", "medium", "j.py", 1, "j.py:1", "", "")
    f2 = Finding("HD009", "", "batch", "medium", "j.py", 1, "j.py:1", "", "")
    ms = [MeasuredFinding(f1, "s", 100.0, 1000.0, 99.0, 990.0, ""),
          MeasuredFinding(f2, "s", 100.0, 1000.0, 50.0, 500.0, "")]
    window, month = combined_saving(ms)
    assert window == pytest.approx(100 * (1 - 0.01 * 0.5))
    assert month == pytest.approx(1000 * (1 - 0.01 * 0.5))


# ----------------------------------------------------------------------------- end to end on the demo


def _generate(tmp_path) -> Path:
    spec = importlib.util.spec_from_file_location("gen", ROOT / "examples/demo_app/logs/generate_logs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    out = tmp_path / "usage.jsonl"
    lines = gen.generate()
    assert gen.generate() == lines  # deterministic
    out.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    return out


def test_measure_cli_on_demo(tmp_path):
    logs = _generate(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["measure", str(logs), "--scan", str(ROOT / "examples/demo_app"), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    tasks = data["baseline"]["tasks"]
    assert tasks["with_outcome"] > 400 and tasks["cost_per_success"]["value"] > 0
    assert data["baseline"]["total"]["tokens"]["cache_hit_rate"] == 0.0  # the demo never caches
    priced = {f["finding"]["rule"]: f for f in data["findings"] if f["saving"]}
    assert {"HD002", "HD006", "HD008", "HD009"} <= set(priced)
    simple_sum = sum(f["saving_month"] for f in data["findings"] if f["saving_month"])
    assert 0 < data["combined_saving"]["month"] < simple_sum  # overlap removed

    text = runner.invoke(app, ["measure", str(logs), "--scan", str(ROOT / "examples/demo_app")]).output
    assert "Cost per successful task" in text and "Combined potential" in text


def test_measure_without_outcomes_says_so(tmp_path):
    f = tmp_path / "u.jsonl"
    f.write_text(json.dumps(ANTHROPIC) + "\n")
    out = CliRunner().invoke(app, ["measure", str(f)]).output
    assert "can't be computed" in out
