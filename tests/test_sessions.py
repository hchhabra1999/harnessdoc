"""Claude Code session-log analysis. Fixtures are synthetic but follow the real record structure
(assistant records with message.id + usage, one record per content block, subagents in their own files)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessdoc.cli import app
from harnessdoc.pricing import PriceBook
from harnessdoc.sessions import analyse, parse_message, parse_since, price_message, read_messages

BOOK = PriceBook.load()
SECRET = "SECRET-PROMPT-TEXT-do-not-leak"


def record(mid="msg_1", model="claude-opus-5-5", session="s1", ts="2026-11-02T10:00:00.000Z", cwd="/work/app",
           inp=1000, out=100, read=2000, w5m=200, w1h=300, speed="standard", rtype="assistant", **extra):
    return {
        "type": rtype, "sessionId": session, "timestamp": ts, "cwd": cwd, "isSidechain": False,
        "message": {"id": mid, "model": model, "role": "assistant",
                    "content": [{"type": "text", "text": SECRET}],
                    "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": w5m + w1h,
                              "cache_creation": {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h},
                              "speed": speed, "service_tier": "standard"}},
        **extra,
    }


def write(path: Path, records: list, junk: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + junk, encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- parsing


def test_parse_message_splits_cache_write_ttl():
    m = parse_message(record(w5m=200, w1h=300), subagent=False)
    assert (m.usage.uncached_input, m.usage.cache_read, m.usage.cache_write, m.write_1h) == (1000, 2000, 200, 300)
    assert m.usage.output == 100 and m.model == "claude-opus-5-5"


def test_parse_message_without_ttl_breakdown_counts_writes_as_5m():
    rec = record()
    del rec["message"]["usage"]["cache_creation"]
    m = parse_message(rec, subagent=False)
    assert (m.usage.cache_write, m.write_1h) == (500, 0)


@pytest.mark.parametrize("rec", [
    record(model="<synthetic>"),  # placeholder messages Claude Code writes itself
    record(inp=0, out=0, read=0, w5m=0, w1h=0),  # nothing billed
    record(rtype="user"),
    {"type": "assistant", "message": {"id": "x", "model": "claude-opus-5-5", "content": []}},  # no usage
])
def test_records_without_billable_usage_are_ignored(rec):
    assert parse_message(rec, subagent=False) is None


# ----------------------------------------------------------------------------- pricing


def test_price_message_prices_each_ttl_separately():
    m = parse_message(record(), subagent=False)
    parts, fast_unpriced = price_message(m, BOOK)
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, writes 1.25x (5m) and 2x (1h) of input
    assert parts.uncached_input == pytest.approx(1000 * 4 / 1e6)
    assert parts.output == pytest.approx(100 * 20 / 1e6)
    assert parts.cache_read == pytest.approx(2000 * 0.20 / 1e6)
    assert parts.cache_write_5m == pytest.approx(200 * 5 / 1e6)
    assert parts.cache_write_1h == pytest.approx(300 * 8 / 1e6)
    assert not fast_unpriced


def test_fast_mode_uses_the_fast_price_ratio_and_flags_unpriced_models():
    standard = price_message(parse_message(record(), False), BOOK)[0].total
    fast = price_message(parse_message(record(speed="fast"), False), BOOK)
    assert fast[0].total == pytest.approx(standard * 2) and not fast[1]  # Opus 5.5 fast = $8 vs $4 input
    # sonnet has no fast-mode price on file: priced at standard, and reported
    s_std = price_message(parse_message(record(model="claude-sonnet-5-5"), False), BOOK)[0].total
    s_fast = price_message(parse_message(record(model="claude-sonnet-5-5", speed="fast"), False), BOOK)
    assert s_fast[0].total == pytest.approx(s_std) and s_fast[1] is True


# ----------------------------------------------------------------------------- reading


def test_each_message_is_counted_once_however_many_blocks_it_was_logged_in(tmp_path):
    # real logs write one record per content block, all carrying the same usage
    write(tmp_path / "proj" / "s1.jsonl", [record("msg_a"), record("msg_a"), record("msg_a"), record("msg_b")])
    msgs, stats = read_messages(tmp_path)
    assert sorted(m.id for m in msgs) == ["msg_a", "msg_b"] and stats["duplicates"] == 2
    r = analyse(tmp_path, BOOK)
    assert r.messages == 2 and r.duplicate_records == 2
    one = price_message(parse_message(record(), False), BOOK)[0].total
    assert r.total.cost.total == pytest.approx(2 * one)  # not 4x


def test_subagent_files_are_read_and_tagged(tmp_path):
    write(tmp_path / "proj" / "s1.jsonl", [record("main")])
    write(tmp_path / "proj" / "s1" / "subagents" / "agent-abc.jsonl", [record("sub1"), record("sub2")])
    r = analyse(tmp_path, BOOK)
    assert (r.files, r.subagent_files, r.messages, r.subagent.messages) == (2, 1, 3, 2)
    assert r.sessions[0].subagent_cost == pytest.approx(r.subagent.cost.total)


def test_bad_lines_are_counted_not_fatal(tmp_path):
    write(tmp_path / "p" / "s.jsonl", [record("m1")], junk='{"truncated": \n\nnot json\n')
    r = analyse(tmp_path, BOOK)
    assert r.messages == 1 and r.unreadable_lines == 2


def test_filters_by_date_and_directory(tmp_path):
    write(tmp_path / "p" / "s.jsonl", [
        record("old", ts="2026-10-01T00:00:00Z", cwd="/work/a"),
        record("mid", ts="2026-11-02T00:00:00Z", cwd="/work/a"),
        record("new", ts="2026-11-05T00:00:00Z", cwd="/work/b"),
    ])
    assert analyse(tmp_path, BOOK, since=parse_since("2026-11-01")).messages == 2
    assert analyse(tmp_path, BOOK, since=parse_since("2026-11-01"), until=parse_since("2026-11-04")).messages == 1
    assert analyse(tmp_path, BOOK, cwd_prefix="/work/b").messages == 1
    assert isinstance(parse_since("3d"), datetime) and parse_since("3d").tzinfo == timezone.utc


def test_unpriced_models_are_listed_and_cost_nothing(tmp_path):
    write(tmp_path / "p" / "s.jsonl", [record("m1", model="claude-future-9"), record("m2")])
    r = analyse(tmp_path, BOOK)
    assert r.unpriced_models == {"claude-future-9": 1} and r.messages == 2
    assert r.total.cost.total == pytest.approx(price_message(parse_message(record(), False), BOOK)[0].total)


def test_breakdown_cache_hit_rate_and_sessions(tmp_path):
    write(tmp_path / "p" / "a.jsonl", [record("m1", session="A", inp=100, read=900, w5m=0, w1h=0),
                                       record("m2", session="B", model="claude-sonnet-5-5")])
    r = analyse(tmp_path, BOOK)
    assert set(r.by_model) == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert r.by_model["claude-opus-5-5"].cache_hit_rate == pytest.approx(0.9)
    assert {s.session for s in r.sessions} == {"A", "B"}
    assert r.sessions[0].cost >= r.sessions[1].cost  # costliest first


# ----------------------------------------------------------------------------- privacy + CLI


def test_prompt_text_never_reaches_the_output(tmp_path):
    write(tmp_path / "p" / "s.jsonl", [record("m1"), record("m2")])
    runner = CliRunner()
    as_json = runner.invoke(app, ["sessions", "--dir", str(tmp_path), "--json"])
    as_text = runner.invoke(app, ["sessions", "--dir", str(tmp_path)])
    assert as_json.exit_code == 0 and as_text.exit_code == 0
    assert SECRET not in as_json.output and SECRET not in as_text.output
    data = json.loads(as_json.output)
    assert data["messages"] == 2 and data["total"]["cost"] > 0
    assert "API-equivalent" in as_text.output and "Where the money goes" in as_text.output


def test_cli_errors(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["sessions", "--dir", str(tmp_path / "missing")]).exit_code == 2
    assert runner.invoke(app, ["sessions", "--dir", str(tmp_path), "--since", "last week"]).exit_code == 2
    assert "No Claude Code usage" in runner.invoke(app, ["sessions", "--dir", str(tmp_path)]).output
