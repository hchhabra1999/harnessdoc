import asyncio
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.goals import Goals, GoalsError, load_goals, save_goals
from harnessdoc.report import analyse, build_report
from harnessdoc.rules import RULES, explain

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_app"


@pytest.fixture
def demo(tmp_path) -> Path:
    """A private copy of the demo app with a generated week of logs and a goals file."""
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns("usage.jsonl", "harnessdoc.goals.yaml", "__pycache__"))
    spec = importlib.util.spec_from_file_location("gen", DEMO / "logs" / "generate_logs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    (dst / "logs" / "usage.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in gen.generate()))
    save_goals(Goals(app="Acme support bot", success="resolved without a human", target_cost_per_success=0.025,
                     task_key="task_id", logs=["logs/usage.jsonl"], constraints=["agent stays on Anthropic"]), dst)
    return dst


# ----------------------------------------------------------------------------- goals


def test_goals_roundtrip(tmp_path):
    g = Goals(app="A bot", success="answer accepted", monthly_tasks=1000, target_cost_per_success=0.01,
              constraints=["stay on OpenAI"], logs=["logs/"])
    path = save_goals(g, tmp_path)
    assert path.name == "harnessdoc.goals.yaml"
    assert path.read_text().startswith("# harnessdoc goals")
    assert load_goals(tmp_path) == g


@pytest.mark.parametrize("data,msg", [
    ({"success": "x"}, "app"),
    ({"app": "x", "target_cost_per_success": -1}, "positive"),
    ({"app": "x", "priorities": ["speed"]}, "priorities"),
    ({"app": "x", "colour": "blue"}, "unknown goals fields"),
    ({"app": "x", "max_success_rate_drop": 150}, "percentage points"),
])
def test_goals_validation(data, msg):
    with pytest.raises(GoalsError, match=msg):
        Goals.from_dict(data)


def test_goals_accepts_single_strings_for_lists():
    g = Goals.from_dict({"app": "x", "logs": "logs/", "constraints": "stay on Anthropic"})
    assert g.logs == ["logs/"] and g.constraints == ["stay on Anthropic"]


def test_invalid_yaml_is_a_goals_error(tmp_path):
    (tmp_path / "harnessdoc.goals.yaml").write_text("app: [unclosed\n")
    with pytest.raises(GoalsError, match="YAML"):
        load_goals(tmp_path)


def test_goals_init_cli(tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["goals", "init", str(tmp_path), "--monthly-tasks", "500"],
                      input="Support bot\nticket resolved\n")
    assert r.exit_code == 0, r.output
    g = load_goals(tmp_path)
    assert (g.app, g.success, g.monthly_tasks) == ("Support bot", "ticket resolved", 500)
    again = runner.invoke(app, ["goals", "init", str(tmp_path), "--app", "x", "--success", "y"])
    assert again.exit_code == 1  # refuses to overwrite without --force


# ----------------------------------------------------------------------------- rules


def test_every_rule_is_documented():
    assert sorted(RULES) == [f"HD{i:03d}" for i in range(1, 11)]
    assert explain("hd002")["rule"] == "HD002"
    with pytest.raises(KeyError):
        explain("HD999")


# ----------------------------------------------------------------------------- report


def test_report_with_logs_and_goals(demo):
    text = build_report(analyse(demo))
    assert text == build_report(analyse(demo))  # deterministic
    for heading in ("## Goal", "## Baseline", "## Recommendations", "## Before changing anything",
                    "## Findings in detail", "## Call sites", "## How these numbers were computed"):
        assert heading in text
    assert "above target" in text and "agent stays on Anthropic" in text
    rec = text.split("## Recommendations")[1].split("##")[0]
    first_row = next(line for line in rec.splitlines() if line.startswith("| 1 |"))
    assert "nightly_summary.py:12" in first_row and "write a guard eval" in first_row
    hd002 = next(line for line in rec.splitlines() if "agent.py:26" in line)
    assert "HD001" in hd002 and "HD003" in hd002 and "guard eval" not in hd002  # caching needs no guard
    assert "Combined potential" in rec


def test_report_without_logs_or_goals(tmp_path):
    shutil.copytree(DEMO, tmp_path / "d", ignore=shutil.ignore_patterns("harnessdoc.goals.yaml", "usage.jsonl"))
    text = build_report(analyse(tmp_path / "d"))
    assert "No `harnessdoc.goals.yaml` yet" in text
    assert "No usage logs given" in text
    assert "~98% of call" in text  # structural estimate still shown


def test_report_cli_writes_file(demo):
    r = CliRunner().invoke(app, ["report", str(demo), "--out", str(demo / "r.md")])
    assert r.exit_code == 0, r.output
    assert (demo / "r.md").read_text().startswith("# harnessdoc report — Acme support bot")


# ----------------------------------------------------------------------------- MCP server

mcp_server = pytest.importorskip("harnessdoc.mcp_server")
from mcp.server.mcpserver.exceptions import ToolError


def call(name: str, args: dict):
    result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_mcp_tools_and_annotations():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(tools) == {"scan", "measure", "price", "explain_rule", "read_goals", "write_goals", "write_report",
                          "compare", "check", "scaffold_guard"}
    writers = {n for n, t in tools.items() if not t.annotations.read_only_hint}
    assert writers == {"write_goals", "write_report", "scaffold_guard"}


def test_mcp_compare_check_and_guard(demo):
    spec = importlib.util.spec_from_file_location("gen", DEMO / "logs" / "generate_logs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    (demo / "logs" / "after.jsonl").write_text(
        "".join(json.dumps(x, sort_keys=True) + "\n" for x in gen.generate(scenario="after")))
    c = call("compare", {"project_path": str(demo), "before": ["logs/usage.jsonl"], "after": ["logs/after.jsonl"]})
    assert c["cost"] == "LOWER" and c["verdict"] in ("SHIP", "INCONCLUSIVE")

    ck = call("check", {"project_path": str(demo)})
    assert ck["baseline_found"] is False and ck["would_fail"] is True

    g = call("scaffold_guard", {"project_path": str(demo), "site": "summarise_closed_tickets"})
    assert g["created"] == ["tests/evals/test_summarise_closed_tickets_guard.py",
                            "tests/evals/summarise_closed_tickets_cases.jsonl"]
    with pytest.raises(ToolError, match="no LLM call site"):
        asyncio.run(mcp_server.server.call_tool("scaffold_guard", {"project_path": str(demo), "site": "nope"}))


def test_mcp_scan_measure_report(demo):
    s = call("scan", {"project_path": str(demo)})
    assert len(s["call_sites"]) == 4 and len(s["uncovered_call_sites"]) == 3
    m = call("measure", {"project_path": str(demo)})  # logs come from the goals file
    assert m["baseline"]["tasks"]["cost_per_success"]["value"] > 0
    assert m["combined_saving"]["month"] > 0
    r = call("write_report", {"project_path": str(demo)})
    assert r["written"] == "harnessdoc-report.md" and (demo / "harnessdoc-report.md").exists()


def test_mcp_goals_roundtrip(tmp_path):
    assert call("read_goals", {"project_path": str(tmp_path)}) == {"exists": False}
    call("write_goals", {"project_path": str(tmp_path), "app": "Bot", "success": "resolved",
                         "logs": ["logs/a.jsonl"]})
    got = call("read_goals", {"project_path": str(tmp_path)})
    assert got["valid"] and got["goals"]["app"] == "Bot"


@pytest.mark.parametrize("name,args,msg", [
    ("measure", {"logs": ["../../../../etc/hosts"]}, "outside the project"),
    ("write_report", {"out": "../escape.md"}, "outside the project"),
    ("write_goals", {"app": "x", "success": "y", "logs": ["/etc/hosts"]}, "outside the project"),
    ("measure", {}, "no logs given"),
])
def test_mcp_refuses_paths_outside_project(tmp_path, name, args, msg):
    with pytest.raises(ToolError, match=msg):
        asyncio.run(mcp_server.server.call_tool(name, {"project_path": str(tmp_path), **args}))
    assert not (tmp_path.parent / "escape.md").exists()


def test_mcp_price_and_explain():
    p = call("price", {"model": "claude-opus-5-5", "input_tokens": 1_000_000})
    assert p["total"] == pytest.approx(4.0)
    assert call("explain_rule", {"rule": "hd009"})["rule"] == "HD009"
    with pytest.raises(ToolError, match="unknown rule"):
        asyncio.run(mcp_server.server.call_tool("explain_rule", {"rule": "HD999"}))
