import importlib.util
import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.compare import compare, newcombe_diff, tasks_needed
from harnessdoc.pricing import PriceBook, Usage
from harnessdoc.usage.records import Outcome, UsageRecord

ROOT = Path(__file__).resolve().parent.parent
BOOK = PriceBook.load()


def test_newcombe_matches_published_example():
    # Newcombe (1998) Table II example (a): 56/70 vs 48/80 -> difference 0.2, method 10 CI 0.0524 to 0.3339.
    # Our convention is p2 - p1, so pass the second group first.
    iv = newcombe_diff(48, 80, 56, 70)
    assert iv.value == pytest.approx(0.2)
    assert iv.low == pytest.approx(0.0524, abs=5e-4)
    assert iv.high == pytest.approx(0.3339, abs=5e-4)


def test_newcombe_edge_cases():
    assert newcombe_diff(0, 0, 5, 10) is None
    iv = newcombe_diff(10, 10, 10, 10)  # both perfect: interval still has width
    assert iv.value == 0 and iv.low < 0 < iv.high


def test_tasks_needed():
    assert tasks_needed(0.84, 0.84, 0.01) == pytest.approx(10_327, rel=0.01)
    assert tasks_needed(0.84, 0.82, 0.01) is None  # observed drop already beyond the margin


def _arm(n_tasks: int, p: float, cost_per_task: float, seed: int, model: str = "claude-sonnet-5-5"):
    """n tasks with exactly round(n·p) successes (deterministic, so tests check the statistics, not luck);
    each task is one request whose input tokens cost ~cost_per_task (noisy). Sonnet has no long-context
    price tier, so token counts map linearly to cost."""
    rng = random.Random(seed)
    price = BOOK.price(model).input
    wins = round(n_tasks * p)
    recs, outs = [], []
    for i in range(n_tasks):
        tokens = max(1, int(rng.gauss(cost_per_task, cost_per_task * 0.2) / price * 1_000_000))
        recs.append(UsageRecord("anthropic", model, Usage(tokens, 0), task_id=f"t{i}", site="answer"))
        outs.append(Outcome(f"t{i}", i < wins))
    return recs, outs


def test_ship_when_cheaper_and_quality_holds():
    c = compare(_arm(4000, 0.85, 0.010, 1), _arm(4000, 0.85, 0.006, 2), BOOK, max_success_rate_drop=3)
    assert (c.quality, c.cost, c.verdict) == ("PASS", "LOWER", "SHIP")
    assert c.cost_change_pct == pytest.approx(-0.4, abs=0.03)


def test_reject_when_quality_clearly_drops():
    c = compare(_arm(4000, 0.85, 0.010, 1), _arm(4000, 0.70, 0.004, 2), BOOK, max_success_rate_drop=2)
    assert (c.quality, c.verdict) == ("FAIL", "REJECT")


def test_reject_when_cost_goes_up():
    c = compare(_arm(4000, 0.85, 0.006, 1), _arm(4000, 0.85, 0.010, 2), BOOK, max_success_rate_drop=3)
    assert (c.cost, c.verdict) == ("HIGHER", "REJECT")


def test_inconclusive_on_small_samples_says_how_many_tasks():
    c = compare(_arm(80, 0.85, 0.010, 1), _arm(80, 0.85, 0.006, 2), BOOK, max_success_rate_drop=1)
    assert c.verdict == "INCONCLUSIVE" and c.quality == "INCONCLUSIVE"
    assert c.tasks_needed_per_window and c.tasks_needed_per_window > 80
    assert "tasks per window" in " ".join(c.reasons)


def test_no_outcomes_is_unknown_not_ship():
    recs, _ = _arm(100, 0.9, 0.01, 1)
    c = compare((recs, []), (recs, []), BOOK)
    assert c.quality == "UNKNOWN" and c.verdict == "INCONCLUSIVE"


def test_site_filter():
    b_recs, b_outs = _arm(500, 0.9, 0.01, 1)
    extra = [UsageRecord("anthropic", "claude-opus-5-5", Usage(10_000, 0), task_id=f"t{i}", site="other")
             for i in range(500)]
    c_all = compare((b_recs + extra, b_outs), (b_recs, b_outs), BOOK)
    c_site = compare((b_recs + extra, b_outs), (b_recs, b_outs), BOOK, site="answer")
    assert c_all.cost == "LOWER"  # dropping the 'other' site's spend
    assert c_site.cost == "NO CLEAR CHANGE"  # same 'answer' traffic on both sides


def _demo_logs(tmp_path):
    spec = importlib.util.spec_from_file_location("gen", ROOT / "examples/demo_app/logs/generate_logs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    paths = []
    for scenario in ("before", "after"):
        p = tmp_path / f"{scenario}.jsonl"
        p.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in gen.generate(scenario=scenario)))
        paths.append(p)
    with pytest.raises(ValueError):
        gen.generate(scenario="sideways")
    return paths


def test_compare_cli_on_demo(tmp_path):
    before, after = _demo_logs(tmp_path)
    runner = CliRunner()
    args = ["compare", "--before", str(before), "--after", str(after), "--project", str(tmp_path)]
    r = runner.invoke(app, [*args, "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["cost"] == "LOWER" and data["cost_change_pct"] < -0.3
    assert data["verdict"] == "INCONCLUSIVE"  # one week can't prove a <1 pt quality bar
    assert data["after"]["cache_hit_rate"] > 0.3 and data["before"]["cache_hit_rate"] == 0

    assert runner.invoke(app, [*args, "--require-ship"]).exit_code == 1
    ship = runner.invoke(app, [*args, "--max-drop", "5", "--require-ship"])
    assert ship.exit_code == 0 and ship.output.startswith("Verdict: SHIP")
