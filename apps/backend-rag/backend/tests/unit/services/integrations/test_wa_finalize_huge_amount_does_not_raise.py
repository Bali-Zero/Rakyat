"""Regression coverage for exact WhatsApp price-token canonicalization."""

from __future__ import annotations

import pytest

from backend.services.integrations.wa_finalize import (
    _canonical_value,
    price_tokens_outside_sources,
)

_HUGE_TOKEN = "9" * 320


def test_canonical_value_handles_a_huge_token_with_a_multiplier() -> None:
    assert _canonical_value(_HUGE_TOKEN, "juta") == int(_HUGE_TOKEN) * 1_000_000


def test_pricing_veto_reports_a_huge_amount_without_raising() -> None:
    assert price_tokens_outside_sources(f"Fee Rp {_HUGE_TOKEN} juta.", []) == [
        f"IDR:{int(_HUGE_TOKEN) * 1_000_000}"
    ]


@pytest.mark.parametrize(
    ("amount", "multiplier", "expected"),
    [
        ("3500", None, 3500),
        ("3.500", "juta", 3_500_000_000),
        ("3,5", "juta", 3_500_000),
        ("3", "k", 3_000),
        ("3", "rb", 3_000),
        ("3", "ribu", 3_000),
        ("3", "thousand", 3_000),
        ("3", "thousands", 3_000),
        ("3", "jt", 3_000_000),
        ("3", "juta", 3_000_000),
        ("3", "mn", 3_000_000),
        ("3", "million", 3_000_000),
        ("3", "millions", 3_000_000),
        ("3", "miliar", 3_000_000_000),
        ("3", "milyar", 3_000_000_000),
        ("3", "bn", 3_000_000_000),
        ("3", "billion", 3_000_000_000),
        ("3", "billions", 3_000_000_000),
        ("3", "triliun", 3_000_000_000_000),
        ("3", "trillion", 3_000_000_000_000),
        ("3", "trillions", 3_000_000_000_000),
    ],
)
def test_canonical_value_preserves_existing_valid_amounts(
    amount: str, multiplier: str | None, expected: int
) -> None:
    assert _canonical_value(amount, multiplier) == expected


def test_canonical_value_returns_none_for_an_unparseable_amount() -> None:
    assert _canonical_value("three million", "juta") is None
