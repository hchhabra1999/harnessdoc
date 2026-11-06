"""Benchmark: the scanner against the planted waste in examples/demo_app (answer key: WASTE.md).

Targets from the PRD: recall >= 9/10 planted issues (W01–W10) and <= 1 false positive; W11 is the
eval-coverage map (only the router is covered).
"""

from pathlib import Path

from harnessdoc.scan import scan_project

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_app"

# id -> (file, acceptable lines, rule)
EXPECTED = {
    "W01": ("acme_support/agent.py", {15}, "HD001"),
    "W02": ("acme_support/agent.py", {26}, "HD002"),
    "W03": ("acme_support/tools.py", {36}, "HD003"),
    "W04": ("acme_support/agent.py", {24, 34}, "HD004"),
    "W05": ("acme_support/agent.py", {24}, "HD005"),
    "W06": ("acme_support/router.py", {11, 12}, "HD006"),
    "W07": ("acme_support/router.py", {13}, "HD007"),
    "W08": ("acme_support/extract.py", {9}, "HD008"),
    "W09": ("jobs/nightly_summary.py", {10, 11}, "HD009"),
    "W10": ("jobs/nightly_summary.py", {12}, "HD006"),
}


def score():
    result = scan_project(DEMO)
    findings = [f for f in result.findings if f.rule != "HD010"]
    matched, used = {}, set()
    for wid, (file, lines, rule) in EXPECTED.items():
        for i, f in enumerate(findings):
            if i not in used and f.rule == rule and f.file == file and f.line in lines:
                matched[wid] = f
                used.add(i)
                break
    false_positives = [f for i, f in enumerate(findings) if i not in used]
    return result, matched, false_positives


def test_recall_and_precision_on_demo_app():
    _, matched, false_positives = score()
    missed = sorted(set(EXPECTED) - set(matched))
    print(f"\nrecall {len(matched)}/{len(EXPECTED)} · false positives {len(false_positives)} · missed {missed}")
    assert len(matched) >= 9, f"missed: {missed}"
    assert len(false_positives) <= 1, [f"{f.rule} {f.file}:{f.line}" for f in false_positives]


def test_w11_eval_coverage_map():
    result, _, _ = score()
    covered = sorted(s.function for s in result.call_sites if result.coverage.get(s.id))
    uncovered = sorted(f.call_site.split(":")[0] for f in result.findings if f.rule == "HD010")
    assert covered == ["classify_intent"]
    assert uncovered == ["acme_support/agent.py", "acme_support/extract.py", "jobs/nightly_summary.py"]


def test_guard_is_attached_to_findings_on_covered_code():
    _, matched, _ = score()
    assert matched["W06"].guard == ["tests/evals/test_router.py"]
    assert matched["W10"].guard == []
