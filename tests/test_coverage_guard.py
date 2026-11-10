import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.guard import GuardError, scaffold
from harnessdoc.scan import scan_project

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_app"

BOT = """
import anthropic
client = anthropic.Anthropic()

def answer(question):
    return client.messages.create(model="claude-haiku-5-5", max_tokens=100,
                                  messages=[{"role": "user", "content": question}])

def summarise(text):
    return client.messages.create(model="claude-haiku-5-5", max_tokens=100,
                                  messages=[{"role": "user", "content": text}])
"""
HANDLER = """
from app.bot import answer

class Handler:
    def handle(self, ticket):
        return self.reply(ticket)

    def reply(self, ticket):
        return answer(ticket["question"])
"""


def make(tmp_path: Path, tests: dict[str, str]) -> Path:
    files = {"app/__init__.py": "", "app/bot.py": BOT, "app/handler.py": HANDLER, **tests}
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    return tmp_path


def covered(root: Path) -> dict[str, list[str]]:
    r = scan_project(root)
    return {s.function: r.coverage[s.id] for s in r.call_sites}


def test_test_on_a_caller_covers_the_llm_call(tmp_path):
    root = make(tmp_path, {"tests/test_handler.py": "from app.handler import Handler\n"
                                                    "def test_it():\n    Handler().handle({'question': 'x'})\n"})
    cov = covered(root)
    assert cov["answer"] == ["tests/test_handler.py"]  # Handler.handle -> reply -> answer
    assert cov["summarise"] == []  # nothing reaches it


def test_direct_reference_still_counts(tmp_path):
    root = make(tmp_path, {"tests/test_bot.py": "from app.bot import summarise\n"})
    assert covered(root)["summarise"] == ["tests/test_bot.py"]


def test_demo_coverage_unchanged():
    cov = covered(DEMO)
    assert cov == {"respond": [], "extract_order_id": [], "classify_intent": ["tests/evals/test_router.py"],
                   "summarise_closed_tickets": []}


def test_scaffold_creates_files_and_empty_scaffold_is_not_coverage(tmp_path):
    root = make(tmp_path, {})
    s = scaffold(root, "summarise")
    assert {p.name for p in s.created} == {"test_summarise_guard.py", "summarise_cases.jsonl"}
    text = s.test_file.read_text()
    assert "from app.bot import summarise" in text and '"text": ...' in text
    compile(text, str(s.test_file), "exec")  # valid Python
    assert covered(root)["summarise"] == []  # empty cases file -> still uncovered

    s.cases_file.write_text(json.dumps({"id": "a", "input": {"text": "hi"}, "expected": "x"}) + "\n")
    assert covered(root)["summarise"] == ["tests/evals/test_summarise_guard.py"]

    s.test_file.write_text("# user edits\n" + text)
    again = scaffold(root, "summarise")
    assert again.created == [] and s.test_file.read_text().startswith("# user edits")  # never overwrites


def test_scaffold_errors(tmp_path):
    root = make(tmp_path, {})
    with pytest.raises(GuardError, match="no LLM call site"):
        scaffold(root, "nope")
    with pytest.raises(GuardError, match="outside the project"):
        scaffold(root, "answer", tests_dir="../elsewhere")


def test_scaffolded_guard_runs_under_pytest(tmp_path):
    # stub SDK so the fake app imports without the real anthropic package
    root = make(tmp_path, {"anthropic.py": "class Anthropic:\n    pass\n"})
    s = scaffold(root, "answer")
    # empty cases -> the guard test is skipped, not failed or errored
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(s.test_file)],
                       cwd=root, capture_output=True, text=True, env={"PYTHONPATH": str(root), "PATH": ""},
                       check=False)
    assert "1 skipped" in r.stdout, r.stdout + r.stderr


def test_guard_cli(tmp_path):
    shutil.copytree(DEMO, tmp_path / "d", ignore=shutil.ignore_patterns("*.jsonl", "__pycache__"))
    runner = CliRunner()
    r = runner.invoke(app, ["guard", "scaffold", "extract_order_id", "--project", str(tmp_path / "d")])
    assert r.exit_code == 0, r.output
    assert "Created tests/evals/test_extract_order_id_guard.py" in r.output
    assert "already exists" in runner.invoke(app, ["guard", "scaffold", "acme_support/extract.py:9",
                                                   "--project", str(tmp_path / "d")]).output


# ----------------------------------------------------------------------------- ambiguous names (found on gptme)

TWO_BACKENDS = {
    "pkg/__init__.py": "",
    "pkg/llm_a.py": """
        import anthropic
        client = anthropic.Anthropic()

        def chat(msgs):
            return client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=msgs)
    """,
    "pkg/llm_b.py": """
        import anthropic
        client = anthropic.Anthropic()

        def chat(msgs):
            return client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=msgs)
    """,
}


def test_ambiguous_function_names_need_the_module_too(tmp_path):
    # `chat` exists in two modules. A test that only says `chat(...)` must not cover both of them.
    root = make_files(tmp_path, {**TWO_BACKENDS, "tests/test_a.py": "from pkg.llm_a import chat\n"
                                                                   "def test_a():\n    chat([])\n"})
    r = scan_project(root)
    by_file = {s.file: r.coverage[s.id] for s in r.call_sites}
    assert by_file["pkg/llm_a.py"] == ["tests/test_a.py"]  # mentions `chat` and `llm_a`
    assert by_file["pkg/llm_b.py"] == []  # shares the name only


def test_unique_names_still_reach_through_callers(tmp_path):
    root = make_files(tmp_path, {
        "app/__init__.py": "",
        "app/bot.py": "import anthropic\nclient = anthropic.Anthropic()\n\n"
                      "def ask(q):\n    return client.messages.create(model='claude-haiku-5-5', max_tokens=9, messages=q)\n"
                      "\ndef handle(t):\n    return ask(t)\n",
        "tests/test_bot.py": "from app.bot import handle\ndef test_h():\n    handle([])\n",
    })
    r = scan_project(root)
    assert [r.coverage[s.id] for s in r.call_sites] == [["tests/test_bot.py"]]


def test_coverage_scales_to_many_functions_and_tests(tmp_path):
    # Regression: coverage ran one regex per function per test file (minutes on a 1,100-file repo).
    import time

    files = {"big/__init__.py": ""}
    for i in range(300):
        files[f"big/m{i}.py"] = "\n".join(f"def f{i}_{j}(x):\n    return g{i}_{j}(x)\n" for j in range(20))
    for i in range(300):
        files[f"tests/test_m{i}.py"] = "\n".join(f"def test_{j}():\n    assert f{i}_{j}(1)\n" for j in range(20))
    files["big/llm.py"] = ("import anthropic\nclient = anthropic.Anthropic()\n\n"
                           "def ask(q):\n    return client.messages.create(model='claude-haiku-5-5', max_tokens=9, messages=q)\n")
    root = make_files(tmp_path, files)
    start = time.perf_counter()
    scan_project(root)
    assert time.perf_counter() - start < 20


def make_files(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    return tmp_path
