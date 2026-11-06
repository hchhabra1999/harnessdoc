import json
from datetime import date

import pytest

from harnessdoc.pricing import PriceBook, UnknownModel, Usage, normalise_model_id


@pytest.fixture(scope="module")
def book():
    return PriceBook.load()


# ----------------------------------------------------------------------------- model resolution


@pytest.mark.parametrize(
    "written,expected",
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic.claude-opus-5-5", "claude-opus-5-5"),  # Bedrock prefix
        ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),  # LiteLLM style
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),  # dated snapshot
        ("claude-opus-4-6@20260101", "claude-opus-4-6"),  # Vertex
        ("openai/gpt-6.1-sol", "gpt-6.1-sol"),
        ("models/gemini-2.5-flash", "gemini-2.5-flash"),
        ("GPT-5.4", "gpt-5.4"),
    ],
)
def test_resolve_ids(book, written, expected):
    assert book.resolve_id(written) == expected


def test_no_false_prefix_matches(book):
    # 'gpt-5.4-mini' is a different model, not a dated gpt-5.4 snapshot
    assert book.resolve_id("gpt-5.4-mini") is None
    with pytest.raises(UnknownModel):
        book.price("totally-made-up")


def test_normalise():
    assert normalise_model_id("  Anthropic.Claude-Opus-5-5 ") == "claude-opus-5-5"


# ----------------------------------------------------------------------------- Anthropic


def test_anthropic_plain_cost(book):
    # Opus 5.5: $4 in / $20 out per 1M
    c = book.cost("claude-opus-5-5", Usage(uncached_input=1_000_000, output=100_000))
    assert c.uncached_input == pytest.approx(4.0)
    assert c.output == pytest.approx(2.0)
    assert c.total == pytest.approx(6.0)


def test_anthropic_cache_read_uses_model_specific_rate(book):
    # Opus 5.5 cache reads are $0.20 (0.05x), not the default 0.1x
    c = book.cost("claude-opus-5-5", Usage(cache_read=1_000_000))
    assert c.cache_read == pytest.approx(0.20)
    # Sonnet 4.6 has no explicit rate -> default 0.1x of $3
    c = book.cost("claude-sonnet-4-6", Usage(cache_read=1_000_000))
    assert c.cache_read == pytest.approx(0.30)


def test_anthropic_cache_write_ttls(book):
    five = book.cost("claude-sonnet-5-5", Usage(cache_write=1_000_000))
    hour = book.cost("claude-sonnet-5-5", Usage(cache_write=1_000_000, cache_write_ttl="1h"))
    assert five.cache_write == pytest.approx(2.0 * 1.25)
    assert hour.cache_write == pytest.approx(2.0 * 2.0)
    with pytest.raises(ValueError, match="TTL"):
        book.cost("claude-sonnet-5-5", Usage(cache_write=1, cache_write_ttl="12h"))


def test_caching_break_even_matches_docs(book):
    # Anthropic docs: with 5m TTL, two requests already break even (1.25 + 0.1 < 2.0)
    prefix = 10_000
    uncached = book.cost("claude-sonnet-4-6", Usage(uncached_input=2 * prefix, requests=2)).total
    cached = (book.cost("claude-sonnet-4-6", Usage(cache_write=prefix)).total
              + book.cost("claude-sonnet-4-6", Usage(cache_read=prefix)).total)
    assert cached < uncached


def test_batch_halves_everything_including_cache(book):
    u = Usage(uncached_input=1000, output=1000, cache_read=1000, cache_write=1000)
    full = book.cost("claude-opus-5-5", u)
    half = book.cost("claude-opus-5-5", Usage(1000, 1000, 1000, 1000, batch=True))
    assert half.total == pytest.approx(full.total * 0.5)


def test_haiku_long_context_tier(book):
    short = book.cost("claude-haiku-5-5", Usage(uncached_input=100_000))
    long_ = book.cost("claude-haiku-5-5", Usage(uncached_input=100_001))
    assert not short.long_context and short.uncached_input == pytest.approx(0.01)
    assert long_.long_context and long_.uncached_input == pytest.approx(100_001 * 0.50 / 1e6)


def test_long_context_threshold_is_per_request(book):
    # 10 requests of 50K each must not trip the 100K per-request tier
    c = book.cost("claude-haiku-5-5", Usage(uncached_input=500_000, requests=10))
    assert not c.long_context


# ----------------------------------------------------------------------------- OpenAI


def test_openai_usage_normalisation():
    # OpenAI input_tokens includes cached reads and writes
    u = Usage.from_openai(input_tokens=10_000, output_tokens=500, cached_tokens=6_000, cache_write_tokens=1_000)
    assert (u.uncached_input, u.cache_read, u.cache_write, u.output) == (3_000, 6_000, 1_000, 500)


def test_openai_explicit_cache_writes_on_new_models(book):
    c = book.cost("gpt-6.1-sol", Usage(cache_write=1_000_000))
    assert c.cache_write == pytest.approx(2.0 * 1.25)  # matches $2.50 on the pricing page
    c = book.cost("gpt-6.1-sol", Usage(cache_write=1_000_000, cache_write_ttl="12h"))
    assert c.cache_write == pytest.approx(5.0)


def test_openai_older_models_bill_writes_as_input(book):
    c = book.cost("gpt-5.4", Usage(cache_write=1_000_000))
    assert c.cache_write == pytest.approx(2.5)


def test_models_without_cache_discount_bill_reads_as_input(book):
    c = book.cost("gpt-5.5-pro", Usage(cache_read=1_000_000))
    assert c.cache_read == pytest.approx(30.0)


# ----------------------------------------------------------------------------- Gemini


def test_gemini_usage_counts_thinking_as_output():
    u = Usage.from_gemini(prompt_token_count=5_000, candidates_token_count=200,
                          cached_content_token_count=4_000, thoughts_token_count=300)
    assert (u.uncached_input, u.cache_read, u.output) == (1_000, 4_000, 500)


def test_gemini_price_schedule(book):
    before = book.price("gemini-4-argon", on=date(2026, 11, 30))
    after = book.price("gemini-4-argon", on=date(2026, 12, 1))
    assert (before.input, before.output) == (2.0, 10.0)
    assert (after.input, after.output, after.cache_read) == (4.0, 20.0, 0.20)


def test_gemini_flash_has_no_batch_discount(book):
    u = Usage(uncached_input=1_000_000, batch=True)
    assert book.cost("gemini-3.8-flash", u, on=date(2026, 11, 6)).total == pytest.approx(0.75)
    assert book.cost("gemini-2.5-flash-lite", u).total == pytest.approx(0.05)


# ----------------------------------------------------------------------------- overrides


def test_project_override_adds_and_changes_models(tmp_path):
    (tmp_path / "harnessdoc.pricing.json").write_text(json.dumps({
        "providers": {
            "openai": {"models": {"gpt-internal-ft": {"input": 1.0, "output": 2.0}}},
            "anthropic": {"models": {"claude-opus-5-5": {"input": 3.0}}},
        }
    }))
    book = PriceBook.load(tmp_path)
    assert book.cost("gpt-internal-ft", Usage(uncached_input=1_000_000)).total == pytest.approx(1.0)
    p = book.price("claude-opus-5-5")
    assert p.input == 3.0 and p.output == 20.0  # deep-merged, untouched fields kept


def test_usage_addition():
    a = Usage(10, 1, 2, 3)
    b = Usage(5, 1, 0, 0)
    assert a + b == Usage(15, 2, 2, 3, requests=2)
    with pytest.raises(ValueError):
        _ = a + Usage(1, batch=True)
