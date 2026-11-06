"""Experiment runner tests. Most use a fake SDK module so no network or API key is needed; one goes through
the real Anthropic SDK with a mock HTTP transport."""

import json
import os
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.experiment import (
    ExperimentError,
    Spec,
    load_cases,
    make_check,
    plan_experiment,
    run_experiment,
)
from harnessdoc.interceptor import Interceptor, Target, Variant

FAKE_TARGETS = (
    Target("fakeai", "Messages", "create", "anthropic", "messages"),
    Target("fakeai", "AsyncMessages", "create", "anthropic", "messages"),
)

FAKE_SDK = '''
class Messages:
    def create(self, **kw):
        tokens = 1000 if "{" in str(kw.get("messages")) else 900
        return {"id": "msg_x", "type": "message", "model": kw["model"],
                "usage": {"input_tokens": tokens, "output_tokens": 5},
                "_label": "wrong" if kw["model"] == "claude-haiku-5-5" and "tricky" in str(kw["messages"]) else None}

class AsyncMessages:
    async def create(self, **kw):
        return Messages().create(**kw)
'''

APP = '''
import os
import fakeai

def classify(message):
    r = fakeai.Messages().create(model="claude-opus-5-5", max_tokens=16000,
                                 messages=[{"role": "user", "content": message}])
    if r["_label"]:
        return r["_label"]
    return "refund" if "refund" in message else "other"

async def classify_async(message):
    r = await fakeai.AsyncMessages().create(model="claude-opus-5-5", messages=[{"role": "user", "content": message}])
    return "refund" if "refund" in message else "other"

def read_env(message):
    fakeai.Messages().create(model="claude-opus-5-5", messages=[])
    return os.environ.get("HD_TEST_MODE", "unset")

def stubborn(message):
    while True:  # an app that swallows every exception and retries forever
        try:
            fakeai.Messages().create(model="claude-opus-5-5", messages=[])
        except Exception:
            continue
'''


def project(tmp_path: Path, n_cases: int = 40, tricky: int = 8) -> Path:
    (tmp_path / "fakeai.py").write_text(FAKE_SDK)
    (tmp_path / "app.py").write_text(APP)
    lines = []
    for i in range(n_cases):
        msg = ("tricky " if i < tricky else "") + ("I want a refund" if i % 2 else "where is my parcel")
        lines.append(json.dumps({"id": f"c{i}", "input": {"message": msg},
                                 "expected": "refund" if i % 2 else "other"}))
    (tmp_path / "cases.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


def spec(**over) -> Spec:
    data = {"name": "router", "call": "app:classify", "cases": "cases.jsonl",
            "variants": {"baseline": {}, "haiku": {"model_overrides": {"claude-opus-5-5": "claude-haiku-5-5"}}},
            "max_success_rate_drop": 2}
    data.update(over)
    return Spec.from_dict(data)


# ----------------------------------------------------------------------------- spec / cases / checks


@pytest.mark.parametrize("data,msg", [
    ({"name": "x", "call": "app:f", "cases": "c"}, "missing"),
    ({"name": "x", "call": "app.f", "cases": "c", "variants": {"a": {}, "b": {}}}, "module:function"),
    ({"name": "x", "call": "app:f", "cases": "c", "variants": {"only": {}}}, "at least two"),
    ({"name": "x", "call": "app:f", "cases": "c", "variants": {"a": {}, "b": {"temperature": 0}}}, "unknown keys"),
    ({"name": "x", "call": "app:f", "cases": "c", "variants": {"a": {}, "b": {}}, "check": "fuzzy"}, "check"),
])
def test_spec_validation(data, msg):
    with pytest.raises(ExperimentError, match=msg):
        Spec.from_dict(data)


def test_load_cases_validation(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text('{"input": {"a": 1}}\n')
    with pytest.raises(ExperimentError, match="expected"):
        load_cases(f)
    f.write_text("")
    with pytest.raises(ExperimentError, match="no cases"):
        load_cases(f)
    f.write_text('{"input": {"a": 1}, "expected": 2}\n')
    assert load_cases(f)[0]["id"] == "case-1"


def test_checks():
    assert make_check("exact")("a", "a") and not make_check("exact")("a ", "a")
    assert make_check("normalized")("  Refund\n", "refund")
    assert make_check("contains")("Your refund is on its way", "REFUND")
    assert make_check("regex")("ORD-123456", r"ORD-\d{6}")


def test_variant_apply_by_api():
    v = Variant("v", {"*": "small"}, max_tokens=50, kwargs={"cache_control": {"type": "ephemeral"}})
    msgs = Target("m", "C", "create", "anthropic", "messages")
    chat = Target("m", "C", "create", "openai", "chat")
    assert v.apply(msgs, {"model": "big"}) == {"model": "small", "max_tokens": 50,
                                               "cache_control": {"type": "ephemeral"}}
    assert v.apply(chat, {"model": "big", "max_tokens": 9})["max_tokens"] == 50  # legacy field kept
    assert v.apply(chat, {"model": "big"})["max_completion_tokens"] == 50


# ----------------------------------------------------------------------------- running (fake SDK)


def test_run_rejects_a_cheaper_variant_that_breaks_quality(tmp_path):
    root = project(tmp_path)
    r = run_experiment(spec(), root, budget_usd=5, targets=FAKE_TARGETS)
    base, haiku = r.variants
    assert (base.passed, base.runs) == (40, 40)
    assert (haiku.passed, haiku.runs) == (32, 40)  # wrong on the 8 tricky cases
    assert haiku.cost < base.cost / 10
    assert haiku.verdict["cost"] == "LOWER" and haiku.verdict["verdict"] == "REJECT"
    assert r.sdk_targets_patched == 2 and not r.stopped_early
    assert r.spent_usd == pytest.approx(base.cost + haiku.cost)


def test_run_ships_when_quality_holds(tmp_path):
    root = project(tmp_path, n_cases=400, tricky=0)
    r = run_experiment(spec(), root, budget_usd=50, targets=FAKE_TARGETS)
    assert r.variants[1].verdict["verdict"] == "SHIP"


def test_budget_stops_before_overspending_and_keeps_cases_paired(tmp_path):
    root = project(tmp_path)
    per_call = 1000 * 4 / 1e6 + 5 * 20 / 1e6  # opus input+output for one baseline call
    r = run_experiment(spec(), root, budget_usd=per_call * 5, targets=FAKE_TARGETS)
    assert r.stopped_early
    assert r.spent_usd < per_call * 5 + per_call  # overshoot bounded by one call
    runs = {v.name: v.runs for v in r.variants}
    assert runs["baseline"] == runs["haiku"] > 0  # only fully-run cases are compared


def test_budget_cannot_be_swallowed_by_app_retry_loops(tmp_path):
    root = project(tmp_path, n_cases=2)
    r = run_experiment(spec(call="app:stubborn"), root, budget_usd=0.01, targets=FAKE_TARGETS)
    assert r.stopped_early  # returned instead of looping forever


def test_env_is_set_per_variant_and_restored(tmp_path):
    root = project(tmp_path, n_cases=2)
    s = spec(call="app:read_env", check="exact",
             variants={"baseline": {}, "flagged": {"env": {"HD_TEST_MODE": "on"}}})
    (root / "cases.jsonl").write_text('{"id": "a", "input": {"message": "x"}, "expected": "on"}\n')
    r = run_experiment(s, root, budget_usd=1, targets=FAKE_TARGETS)
    assert [v.passed for v in r.variants] == [0, 1]
    assert "HD_TEST_MODE" not in os.environ


def test_async_functions_and_async_sdk_methods(tmp_path):
    root = project(tmp_path, n_cases=4, tricky=0)
    r = run_experiment(spec(call="app:classify_async"), root, budget_usd=1, targets=FAKE_TARGETS)
    assert all(v.passed == v.runs == 4 for v in r.variants)
    assert all(v.unmeasured_calls == 0 and v.calls == 4 for v in r.variants)


def test_patches_are_removed_after_a_run(tmp_path):
    root = project(tmp_path, n_cases=2)
    run_experiment(spec(), root, budget_usd=1, targets=FAKE_TARGETS)
    import fakeai

    assert fakeai.Messages.create.__qualname__ == "Messages.create"


def test_plan_estimates_only_with_logs(tmp_path):
    root = project(tmp_path)
    p = plan_experiment(spec(), root)
    assert p.runs == 80 and p.est_cost_usd is None and "pilot" in p.basis
    p = plan_experiment(spec(), root, avg_cost_per_call=0.004, baseline_model="claude-opus-5-5")
    assert p.est_cost_usd == pytest.approx(40 * 0.004 + 40 * 0.004 * (0.6 / 24))
    assert p.suggested_budget_usd >= p.est_cost_usd


# ----------------------------------------------------------------------------- real SDK, mock transport


def test_interceptor_through_the_real_anthropic_sdk():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen.update(body)
        return httpx2.Response(200, json={
            "id": "msg_t", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 3, "cache_read_input_tokens": 50,
                      "cache_creation_input_tokens": 0}})

    client = anthropic.Anthropic(api_key="test",
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    calls = []
    with Interceptor(Variant("v", {"claude-opus-5-5": "claude-haiku-5-5"},
                             kwargs={"cache_control": {"type": "ephemeral"}}), calls.append):
        client.messages.create(model="claude-opus-5-5", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert seen["model"] == "claude-haiku-5-5" and seen["cache_control"] == {"type": "ephemeral"}
    assert calls[0].model == "claude-haiku-5-5"
    assert (calls[0].usage.uncached_input, calls[0].usage.cache_read) == (120, 50)
    assert anthropic.resources.messages.messages.Messages.create.__qualname__ == "Messages.create"


# ----------------------------------------------------------------------------- CLI


def test_demo_experiment_spec_plans_with_measured_cost(tmp_path):
    import importlib.util
    import shutil

    demo = Path(__file__).resolve().parent.parent / "examples" / "demo_app"
    root = tmp_path / "demo"
    shutil.copytree(demo, root, ignore=shutil.ignore_patterns("*.json", "usage*.jsonl", "__pycache__"))
    gen_spec = importlib.util.spec_from_file_location("gen", demo / "logs" / "generate_logs.py")
    gen = importlib.util.module_from_spec(gen_spec)
    gen_spec.loader.exec_module(gen)
    (root / "logs" / "usage.jsonl").write_text("".join(json.dumps(x) + "\n" for x in gen.generate()))
    out = CliRunner().invoke(app, ["experiment", "plan", str(root / "harnessdoc.experiment.yaml"),
                                   "--project", str(root)])
    assert out.exit_code == 0, out.output
    assert "24 cases × 1 repeat(s) × 3 variants = 72 runs" in out.output
    assert "Estimated cost: $" in out.output and "measured $" in out.output


def test_cli_plan_and_refusal_without_confirmation(tmp_path):
    root = project(tmp_path)
    spec_file = root / "exp.yaml"
    spec_file.write_text(textwrap.dedent("""
        name: router
        call: app:classify
        cases: cases.jsonl
        variants:
          baseline: {}
          haiku: {model_overrides: {claude-opus-5-5: claude-haiku-5-5}}
    """))
    runner = CliRunner()
    p = runner.invoke(app, ["experiment", "plan", str(spec_file), "--project", str(root)])
    assert p.exit_code == 0 and "80 runs" in p.output and "pilot" in p.output
    r = runner.invoke(app, ["experiment", "run", str(spec_file), "--project", str(root), "--budget", "1"],
                      input="n\n")
    assert r.exit_code == 1 and "costs money" in r.output
    assert not list(root.glob("harnessdoc-experiment-*.json"))


# ----------------------------------------------------------------------------- unpriced models stop the run


def test_unpriced_model_halts_the_run_because_budget_is_unenforceable(tmp_path):
    root = project(tmp_path)
    s = spec(variants={"baseline": {}, "mystery": {"model_overrides": {"claude-opus-5-5": "mystery-model-1"}}})
    r = run_experiment(s, root, budget_usd=5, targets=FAKE_TARGETS)
    assert r.stopped_early and "mystery-model-1" in r.stop_reason and "can't be enforced" in r.stop_reason
    assert r.unpriced_models == ["mystery-model-1"]
    assert r.variants[0].runs == r.variants[1].runs  # only fully-paired cases are kept
    assert sum(v.calls for v in r.variants) <= 4  # stopped within the first case, not after 80 runs


def test_allow_unpriced_keeps_running(tmp_path):
    root = project(tmp_path, n_cases=3)
    s = spec(variants={"baseline": {}, "mystery": {"model_overrides": {"claude-opus-5-5": "mystery-model-1"}}})
    r = run_experiment(s, root, budget_usd=5, targets=FAKE_TARGETS, allow_unpriced=True)
    assert not r.stopped_early and r.variants[1].runs == 3 and r.variants[1].cost == 0


def test_minimax_models_are_in_the_price_book():
    from harnessdoc.pricing import PriceBook, Usage

    book = PriceBook.load()
    assert book.resolve_id("MiniMax-M3") == "minimax-m3"
    assert book.resolve_id("MiniMax-M3.1-Flash") == "minimax-m3.1-flash"
    # 400K tokens of each kind in one request stays under the 512K long-context threshold
    # (a request with 1M input tokens would correctly price at the doubled tier)
    c = book.cost("MiniMax-M3", Usage(uncached_input=200_000, output=400_000, cache_read=200_000))
    assert not c.long_context
    assert c.uncached_input == pytest.approx(0.06) and c.output == pytest.approx(0.48)
    assert c.cache_read == pytest.approx(0.012)
    assert book.cost("MiniMax-M3.1-Flash", Usage(uncached_input=1_000_000)).total == 0.15
    assert book.cost("MiniMax-M3", Usage(uncached_input=512_001)).long_context  # >512K tier


def test_minimax_experiment_spec_loads_and_plans(tmp_path):
    import shutil

    demo = Path(__file__).resolve().parent.parent / "examples" / "demo_app"
    root = tmp_path / "demo"
    shutil.copytree(demo, root, ignore=shutil.ignore_patterns("*.jsonl", "__pycache__"))
    (root / "tests" / "evals").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "evals" / "classify_intent_cases.jsonl").write_text(
        (demo / "tests" / "evals" / "classify_intent_cases.jsonl").read_text())
    s = Spec.load(root / "harnessdoc.experiment.minimax.yaml")
    assert [v.name for v in s.variants] == ["baseline", "flash", "flash-capped"]
    assert plan_experiment(s, root).runs == 72
