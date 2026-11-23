"""Visa Clock chat preamble uses the same expiry rule as the clock timeline."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.services.visa_check.catalogue import VisaType
from backend.services.visa_check.clock import clock_timeline
from backend.services.visa_unified.bridge import FunnelContext, augment_chat_system_prompt


def _clock_context(*, entry_date, expiry_date, visa_type="B1") -> FunnelContext:
    return FunnelContext(
        check_hash="clock-expiry-test",
        nationality="",
        purpose="",
        duration_months=0,
        budget_band="",
        recommended_visa=None,
        estimated_cost_idr=None,
        alternatives=[],
        referral_mode=False,
        branch="clock",
        visa_type=visa_type,
        entry_date=entry_date,
        expiry_date=expiry_date,
        extensions_possible=1,
        extension_days=30,
    )


def test_clock_preamble_recomputes_legacy_b1_expiry_from_entry_date():
    today = datetime.now(timezone.utc).date()
    entry_date = today - timedelta(days=29)
    legacy_expiry = entry_date + timedelta(days=30)

    out = augment_chat_system_prompt(
        _clock_context(entry_date=entry_date, expiry_date=legacy_expiry), "base"
    )

    assert f"permitted stay ends {today.isoformat()} (ends today)" in out
    assert legacy_expiry.isoformat() not in out


def test_clock_preamble_keeps_matching_expiry_text_unchanged():
    today = datetime.now(timezone.utc).date()
    entry_date = today - timedelta(days=29)

    out = augment_chat_system_prompt(
        _clock_context(entry_date=entry_date, expiry_date=today), "base"
    )

    assert f"permitted stay ends {today.isoformat()} (ends today)" in out


@pytest.mark.parametrize("visa_type", list(VisaType))
def test_clock_preamble_quotes_the_clock_page_expiry_for_every_visa_type(visa_type):
    entry_date = datetime.now(timezone.utc).date() - timedelta(days=3)
    stale_expiry = entry_date + timedelta(days=400)
    expected = clock_timeline(visa_type=visa_type, entry_date=entry_date).expiry_date

    out = augment_chat_system_prompt(
        _clock_context(entry_date=entry_date, expiry_date=stale_expiry, visa_type=visa_type.value),
        "base",
    )

    assert f"permitted stay ends {expected.isoformat()}" in out
    assert stale_expiry.isoformat() not in out


def test_clock_preamble_keeps_the_stored_expiry_for_an_unknown_visa_type():
    today = datetime.now(timezone.utc).date()
    stored = today + timedelta(days=10)

    out = augment_chat_system_prompt(
        _clock_context(
            entry_date=today - timedelta(days=5), expiry_date=stored, visa_type="NOT-A-TYPE"
        ),
        "base",
    )

    assert f"permitted stay ends {stored.isoformat()} (10 days remaining)" in out


@pytest.mark.parametrize(
    ("entry_date", "visa_type"),
    [(date.min, VisaType.B1), (date.min, VisaType.D2)]
    + [(date.max, visa_type) for visa_type in VisaType],
)
def test_clock_preamble_keeps_the_stored_expiry_when_the_timeline_cannot_be_computed(
    entry_date, visa_type
):
    with pytest.raises(OverflowError):
        clock_timeline(visa_type=visa_type, entry_date=entry_date)
    stored = datetime.now(timezone.utc).date() + timedelta(days=10)

    out = augment_chat_system_prompt(
        _clock_context(entry_date=entry_date, expiry_date=stored, visa_type=visa_type.value),
        "base",
    )

    assert f"permitted stay ends {stored.isoformat()} (10 days remaining)" in out
