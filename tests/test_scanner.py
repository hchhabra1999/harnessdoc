"""Scanner tests: each check has a positive and a negative case; detection covers all four SDKs."""

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.scan import scan_project

POLICY = "You are a helpful support agent. Follow the refund rules carefully. " * 60  # ~1K tokens


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    return tmp_path


def rules(tmp_path: Path, files: dict[str, str]) -> list[str]:
    return sorted(f.rule for f in scan_project(project(tmp_path, files)).findings)


# ----------------------------------------------------------------------------- detection


def test_detects_all_four_sdks(tmp_path):
    root = project(tmp_path, {
        "a.py": """
            import anthropic
            client = anthropic.Anthropic()
            def f(): return client.beta.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=[])
        """,
        "o.py": """
            from openai import OpenAI
            oai = OpenAI()
            def g(): return oai.responses.create(model="gpt-6.1-sol", input="hi", instructions="Be brief.")
        """,
        "gm.py": """
            from google import genai
            from google.genai import types
            client = genai.Client()
            def h(): return client.models.generate_content(
                model="gemini-2.5-flash", contents="hi",
                config=types.GenerateContentConfig(max_output_tokens=50, system_instruction="Be brief."))
        """,
        "l.py": """
            import litellm
            def k(): return litellm.completion(model="anthropic/claude-opus-5-5", messages=[])
        """,
    })
    sites = {s.file: s for s in scan_project(root).call_sites}
    assert sites["a.py"].provider == "anthropic" and sites["a.py"].api == "messages.create"
    assert sites["o.py"].provider == "openai" and sites["o.py"].system.value == "Be brief."
    assert sites["gm.py"].provider == "gemini" and sites["gm.py"].max_tokens == 50
    assert sites["gm.py"].system.value == "Be brief."
    assert sites["l.py"].provider == "litellm" and sites["l.py"].model.value == "anthropic/claude-opus-5-5"


def test_ignores_non_llm_messages_create(tmp_path):
    # Twilio-style client in a file with no LLM SDK import must not count as an LLM call
    root = project(tmp_path, {"sms.py": """
        from twilio.rest import Client
        client = Client()
        def send(): client.messages.create(to="+1", body="hi")
    """})
    assert scan_project(root).call_sites == []


def test_model_constant_and_cross_module_prompt(tmp_path):
    root = project(tmp_path, {
        "app/prompts.py": f'SYSTEM = """{POLICY}"""\n',
        "app/bot.py": """
            import anthropic
            from .prompts import SYSTEM
            MODEL = "claude-sonnet-5-5"
            client = anthropic.Anthropic()
            def ask(q): return client.messages.create(model=MODEL, max_tokens=MAX, system=SYSTEM, messages=[])
            MAX = 512
        """,
    })
    [s] = scan_project(root).call_sites
    assert s.model.value == "claude-sonnet-5-5"
    assert s.max_tokens == 512
    assert s.system.tokens and s.system.tokens > 900


# ----------------------------------------------------------------------------- HD001 / HD002 caching

ANTHROPIC_HEADER = textwrap.dedent("""
    import anthropic
    from datetime import datetime
    client = anthropic.Anthropic()
    POLICY = {policy!r}
""")


def anth(body: str) -> dict[str, str]:
    return {"bot.py": ANTHROPIC_HEADER.format(policy=POLICY) + textwrap.dedent(body)}


def test_hd001_leading_timestamp(tmp_path):
    r = rules(tmp_path, anth("""
    def ask(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100,
            system=f"Now: {datetime.now()}\\n{POLICY}", messages=[{"role": "user", "content": q}])
    """))
    assert "HD001" in r


def test_hd001_trailing_timestamp_is_medium_not_high(tmp_path):
    root = project(tmp_path, anth("""
    def ask(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100,
            system=f"{POLICY}\\nNow: {datetime.now()}", messages=[])
    """))
    [f] = [f for f in scan_project(root).findings if f.rule == "HD001"]
    assert f.severity == "medium"


def test_hd002_negative_when_cached(tmp_path):
    r = rules(tmp_path, anth("""
    def ask(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100,
            system=[{"type": "text", "text": POLICY, "cache_control": {"type": "ephemeral"}}], messages=[])
    def ask2(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, system=POLICY,
            cache_control={"type": "ephemeral"}, messages=[])
    """))
    assert "HD002" not in r


def test_hd002_positive_and_short_prompt_negative(tmp_path):
    r = rules(tmp_path, anth("""
    def ask(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, system=POLICY, messages=[])
    def short(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, system="Be brief.", messages=[])
    """))
    assert r.count("HD002") == 1


def test_hd002_not_for_openai_implicit_caching(tmp_path):
    r = rules(tmp_path, {"o.py": f"""
from openai import OpenAI
client = OpenAI()
POLICY = {POLICY!r}
def ask(q):
    return client.chat.completions.create(model="gpt-6.1-sol", messages=[{{"role": "system", "content": POLICY}}])
"""})
    assert "HD002" not in r


# ----------------------------------------------------------------------------- HD003 tools


@pytest.mark.parametrize("builder,flagged", [
    ("[{'name': n} for n in NAMES]", True),
    ("[{'name': n} for n in sorted(NAMES)]", False),
    ("[{'name': n} for n in ['a', 'b']]", False),
    ("list(NAMES)", True),
])
def test_hd003_set_iteration(tmp_path, builder, flagged):
    r = rules(tmp_path, anth(f"""
    NAMES = {{"lookup", "refund"}}
    def tools():
        return {builder}
    def ask(q):
        return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, tools=tools(), messages=[])
    """))
    assert ("HD003" in r) is flagged


# ----------------------------------------------------------------------------- HD004 / HD005 loops


def test_hd004_hd005_unbounded_loop(tmp_path):
    r = rules(tmp_path, anth("""
    def run(history):
        while True:
            try:
                r = client.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=history)
            except anthropic.APIError:
                continue
            if r.stop_reason != "tool_use":
                return r
    """))
    assert {"HD004", "HD005"} <= set(r)


def test_loops_with_caps_are_fine(tmp_path):
    r = rules(tmp_path, anth("""
    def run(history):
        turns = 0
        while True:
            turns += 1
            if turns > 8:
                return None
            r = client.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=history)
            if r.stop_reason != "tool_use":
                return r
    def retry(history):
        for attempt in range(3):
            try:
                return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=history)
            except anthropic.APIError:
                continue
    """))
    assert "HD004" not in r and "HD005" not in r


def test_except_that_reraises_is_not_a_silent_retry(tmp_path):
    r = rules(tmp_path, anth("""
    def run(history):
        while True:
            try:
                return client.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=history)
            except anthropic.APIError:
                raise
    """))
    assert "HD004" not in r


# ----------------------------------------------------------------------------- HD006 / HD007 model + output


def test_hd006_premium_model_for_classification(tmp_path):
    root = project(tmp_path, anth("""
    def cls(m):
        return client.messages.create(model="claude-opus-5-5", max_tokens=10,
            messages=[{"role": "user", "content": f"Classify this message as spam or ham: {m}"}])
    """))
    [f] = [f for f in scan_project(root).findings if f.rule == "HD006"]
    assert f.est_saving_pct == pytest.approx(1 - 0.6 / 24, abs=1e-3)  # haiku $0.1+$0.5 vs opus $4+$20


def test_hd006_negatives(tmp_path):
    r = rules(tmp_path, anth("""
    def plan(m):  # open-ended task on a premium model: fine
        return client.messages.create(model="claude-opus-5-5", max_tokens=4000,
            messages=[{"role": "user", "content": f"Write a migration plan for: {m}"}])
    def cls(m):  # classification already on a small model: fine
        return client.messages.create(model="claude-haiku-5-5", max_tokens=10,
            messages=[{"role": "user", "content": f"Classify this message as spam or ham: {m}"}])
    def dyn(m, model):  # model not statically known: no claim
        return client.messages.create(model=model, max_tokens=10,
            messages=[{"role": "user", "content": f"Classify this: {m}"}])
    """))
    assert "HD006" not in r


def test_hd007_only_with_short_answer_prompt(tmp_path):
    r = rules(tmp_path, anth("""
    def a(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=8000,
            messages=[{"role": "user", "content": f"Is this spam? Answer yes or no. {m}"}])
    def b(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=8000,
            messages=[{"role": "user", "content": f"Write a detailed report about {m}"}])
    """))
    assert r.count("HD007") == 1


# ----------------------------------------------------------------------------- HD008 / HD009


def test_hd008_regexable_extraction(tmp_path):
    r = rules(tmp_path, anth("""
    def a(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=20,
            messages=[{"role": "user", "content": f"Extract the email address from: {m}"}])
    def b(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=200,
            messages=[{"role": "user", "content": f"Extract the customer's main complaint from: {m}"}])
    """))
    assert r.count("HD008") == 1


def test_hd009_batch_only_for_offline_code(tmp_path):
    body = """
    def {name}(items):
        out = []
        for it in items:
            out.append(client.messages.create(model="claude-haiku-5-5", max_tokens=100,
                messages=[{{"role": "user", "content": it}}]))
        return out
    """
    files = anth(body.format(name="nightly_backfill"))
    files["web.py"] = (ANTHROPIC_HEADER.format(policy="x") + textwrap.dedent(body.format(name="handle_request")))
    found = [f for f in scan_project(project(tmp_path, files)).findings if f.rule == "HD009"]
    assert [f.file for f in found] == ["bot.py"]


# ----------------------------------------------------------------------------- coverage + determinism


def test_eval_coverage_by_function_name(tmp_path):
    files = anth("""
    def covered(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    def uncovered(m):
        return client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    """)
    files["tests/test_bot.py"] = "from bot import covered\n\ndef test_it():\n    assert covered('x')\n"
    result = scan_project(project(tmp_path, files))
    cov = {k.split(":")[0] + ":" + k.split(":")[1]: v for k, v in result.coverage.items()}
    assert sorted(len(v) for v in cov.values()) == [0, 1]
    assert [f.call_site for f in result.findings if f.rule == "HD010"] == [
        s.id for s in result.call_sites if s.function == "uncovered"
    ]


def test_parse_errors_are_reported_not_fatal(tmp_path):
    result = scan_project(project(tmp_path, {"broken.py": "def (:\n", "ok.py": "x = 1\n"}))
    assert result.files_scanned == 2 and len(result.errors) == 1 and "broken.py" in result.errors[0]


def test_json_output_is_deterministic():
    demo = Path(__file__).resolve().parent.parent / "examples" / "demo_app"
    runner = CliRunner()
    a = runner.invoke(app, ["scan", str(demo), "--json"])
    b = runner.invoke(app, ["scan", str(demo), "--json"])
    assert a.exit_code == 0 and a.output == b.output
    assert json.loads(a.output)["findings"]


# ----------------------------------------------------------------------------- injected clients
# Found by running the scanner on a real repo (optillm): modules that take `client` as a parameter and
# import no SDK were invisible, so recall was ~30%.


def test_injected_client_without_sdk_import_is_detected(tmp_path):
    root = project(tmp_path, {"approach.py": """
        import logging

        def run(system_prompt, query, client, model):
            return client.chat.completions.create(model=model, messages=[{"role": "user", "content": query}])

        def run_responses(llm_client, model):
            return llm_client.responses.create(model=model, input="x")

        def run_anthropic(client, model):
            return client.messages.create(model=model, max_tokens=100, messages=[])
    """})
    sites = scan_project(root).call_sites
    assert sorted((s.function, s.provider) for s in sites) == [
        ("run", "openai"), ("run_anthropic", "anthropic"), ("run_responses", "openai")]


def test_injected_client_guard_still_rejects_look_alikes(tmp_path):
    root = project(tmp_path, {"notify.py": """
        def sms(client, to):
            return client.messages.create(to=to, body="hi")  # Twilio: no model / max_tokens

        def slack(client):
            return client.messages.create(channel="#x", text="hi", max_tokens=1)  # no model

        def not_a_client(db, model):
            return db.chat.completions.create(model=model)  # receiver doesn't look like a client
    """})
    assert scan_project(root).call_sites == []
