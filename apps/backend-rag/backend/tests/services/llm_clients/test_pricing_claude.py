"""Claude rows of the price table (Console seats are metered per token).

Until 2026-11-03 the table had no Claude row at all: every Console call was
billed at the ``unknown`` default ($1/$3), so a Fable 5.1 answer ($10/$50)
under-billed 10x and a cold cache write (1.25x input) was not modelled.
"""

from __future__ import annotations

import pytest

from backend.services.llm_clients.pricing import (
    LLM_PRICING,
    calculate_cost,
    create_token_usage,
    resolve_pricing,
)

# (input, output, cache hit, 5m write, 1h write) per MTok — verbatim from
# https://platform.claude.com/docs/about-claude/pricing on 2026-11-03.
_OFFICIAL = {
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5": (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00, 8.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50, 4.00),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25, 2.00),
}


def test_every_claude_row_matches_the_official_price_list():
    """TRIPWIRE: anchor on the price list, not on the table's own past."""
    claude_rows = {k for k in LLM_PRICING if k.startswith("claude-")}
    assert claude_rows == set(_OFFICIAL)
    for slug, (inp, out, hit, w5, w1h) in _OFFICIAL.items():
        row = LLM_PRICING[slug]
        got = (row["input"], row["output"], row["cached_input"], row["cache_write"], row["cache_write_1h"])
        assert got == (inp, out, hit, w5, w1h), slug


@pytest.mark.parametrize(
    ("slug", "row"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-sonnet-4-20250514", "claude-sonnet-4"),
        # A prefix key must never capture its sibling: the longest key wins.
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-opus-5-5-20261001", "claude-opus-5-5"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("claude-fable-5-1", "claude-fable-5-1"),
        ("claude-opus-5", "claude-opus-5"),
        ("claude-sonnet-5", "claude-sonnet-5"),
    ],
)
def test_claude_slugs_resolve_to_their_own_row(slug, row):
    assert resolve_pricing(slug) is LLM_PRICING[row]


@pytest.mark.parametrize(
    "slug",
    [
        # Opus 4 is $15/$75: no prefix row may price it at a 4.x sibling's $5/$25.
        "claude-opus-4-20250514",
        # An abbreviation that names several rows identifies none of them.
        "claude-opus-4",
        "opus",
        "sonnet",
        "claude-fable",
    ],
)
def test_unlisted_or_ambiguous_claude_slugs_refuse_to_guess(slug):
    assert resolve_pricing(slug) is LLM_PRICING["unknown"]


@pytest.mark.parametrize(
    ("model", "prompt", "output", "cli_cost"),
    [
        # Live envelopes on a Console seat, 2026-11-03 (CLI 2.1.316):
        # modelUsage[*].costUSD with costBasis "list".
        ("claude-haiku-4-5", 81, 51, 0.000336),
        ("claude-haiku-4-5", 783, 104, 0.001303),
        ("claude-fable-5-1", 71, 5, 0.00096),
    ],
)
def test_table_agrees_with_the_cli_list_price(model, prompt, output, cli_cost):
    assert calculate_cost(prompt, output, model) == pytest.approx(cli_cost, abs=1e-9)


def test_cache_writes_bill_at_their_own_rates():
    """GUILT: the 18,576-token prefix a cold call wrote cost 1.25x input; the
    table had no write rate, so the write was billed as plain input — or, in
    the Console client, not at all."""
    cost = calculate_cost(
        prompt_tokens=10_000 + 1_000 + 2_000 + 4_000,
        completion_tokens=500,
        model="claude-haiku-4-5",
        cached_tokens=1_000,
        cache_write_tokens=2_000,
        cache_write_1h_tokens=4_000,
    )
    expected = (10_000 * 1.00 + 1_000 * 0.10 + 2_000 * 1.25 + 4_000 * 2.00 + 500 * 5.00) / 1e6
    assert cost == pytest.approx(expected, abs=1e-9)


def test_cache_write_counts_are_clamped_to_the_prompt():
    """Over-reported writes cannot bill more tokens than the prompt carried."""
    cost = calculate_cost(100, 0, "claude-haiku-4-5", cached_tokens=60, cache_write_tokens=500)
    assert cost == pytest.approx((60 * 0.10 + 40 * 1.25) / 1e6, abs=1e-12)


def test_rows_without_write_rates_are_unchanged():
    """INNOCENCE: a Gemini row has no write rate, so a write prices as input —
    exactly what the same tokens cost before this argument existed."""
    before = calculate_cost(10_000, 100, "gemini-2.5-flash", cached_tokens=1_000)
    after = calculate_cost(10_000, 100, "gemini-2.5-flash", cached_tokens=1_000, cache_write_tokens=3_000)
    assert after == before


def test_create_token_usage_prices_writes_like_the_ledger():
    """One call, one price: the spend-cap object sees the writes too."""
    kwargs = {"cached_tokens": 1_000, "cache_write_tokens": 2_000, "cache_write_1h_tokens": 500}
    usage = create_token_usage(5_000, 100, "claude-sonnet-4-6", **kwargs)
    assert usage.cost_usd == calculate_cost(5_000, 100, "claude-sonnet-4-6", **kwargs)
