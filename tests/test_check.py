import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.check import check, read_baseline, write_baseline
from harnessdoc.cli import app
from harnessdoc.scan import scan_project

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_app"
NEW_CALL = '''

def classify_again(m):
    return client.messages.create(model="claude-opus-5-5", max_tokens=9000,
        messages=[{"role": "user", "content": f"Classify this: {m}. Reply with the label only"}])
'''


@pytest.fixture
def demo(tmp_path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns("*.jsonl", "__pycache__"))
    return dst


def run(args):
    return CliRunner().invoke(app, ["check", *map(str, args)])


def test_clean_against_own_baseline(demo):
    base = demo / "harnessdoc-baseline.json"
    assert run([demo, "--write-baseline", base]).exit_code == 0
    r = run([demo, "--baseline", base])
    assert r.exit_code == 0, r.output
    assert "0 new · 13 unchanged · 0 fixed" in r.output


def test_shifted_lines_are_not_new(demo):
    base = write_baseline(scan_project(demo), demo / "b.json")
    router = demo / "acme_support" / "router.py"
    router.write_text("import os\nimport sys\n" + router.read_text())  # every line moves by 2
    res = check(scan_project(demo), read_baseline(base))
    assert res.new == [] and res.fixed == []


def test_new_wasteful_call_fails_the_gate(demo):
    base = write_baseline(scan_project(demo), demo / "b.json")
    with (demo / "acme_support" / "router.py").open("a") as fh:
        fh.write(NEW_CALL)
    r = run([demo, "--baseline", base, "--json"])
    assert r.exit_code == 1
    data = json.loads(r.output)
    rules = sorted(f["rule"] for f in data["new"])
    assert rules == ["HD006", "HD007", "HD010"]  # premium model, oversized cap, no eval
    assert [f.split()[0] for f in data["failing"]] == ["HD006"]  # only medium+ fails; HD007 low, HD010 info
    assert run([demo, "--baseline", base, "--fail-on", "high"]).exit_code == 0  # nothing new is high


def test_fixed_findings_are_reported(demo):
    base = write_baseline(scan_project(demo), demo / "b.json")
    tools = demo / "acme_support" / "tools.py"
    tools.write_text(tools.read_text().replace("for name in _TOOL_NAMES", "for name in sorted(_TOOL_NAMES)"))
    res = check(scan_project(demo), read_baseline(base))
    assert res.new == []
    assert res.fixed == ["HD003:acme_support/tools.py:respond"]


def test_without_baseline_everything_is_new(demo):
    r = run([demo])
    assert r.exit_code == 1 and "found (no baseline)" in r.output


def test_bad_baseline_file(demo, tmp_path):
    (tmp_path / "x.json").write_text('{"not": "a baseline"}')
    assert run([demo, "--baseline", tmp_path / "x.json"]).exit_code == 2
