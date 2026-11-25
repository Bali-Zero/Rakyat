"""D12 can be extended once for 180 days (owner ruling 2026-11-23, decision K134).

This replaces the non-extendable reading of the 2026-10-19 ruling in the legacy
visa check. The official page states the same extension: "Izin tinggal dari visa
ini bisa diperpanjang untuk 180 hari berikutnya".

Guilt: on the previous main the clock offers no extension and a 12-month plan is
reported as longer than the D12 stay.
Innocence: a plan beyond the 360-day total is still reported as too long.
"""

from __future__ import annotations

from datetime import date

from backend.services.visa_check.catalogue import VISA_META, VisaType
from backend.services.visa_check.clock import clock_timeline
from backend.services.visa_check.match_tree import BudgetBand, Purpose, recommend_visa


def _d12_reason(months: int) -> str:
    result = recommend_visa(
        nationality="USA",
        purpose=Purpose.INVESTOR,
        duration_months=months,
        budget_band=BudgetBand.OVER_500M,
    )
    return next(row.reason for row in result.ranking if row.visa is VisaType.D12)


def test_d12_extends_once_for_180_days() -> None:
    meta = VISA_META[VisaType.D12]
    assert meta.extensions == (1, 180)
    assert "extendable once for 180 days" in meta.notes
    assert "non-extendable" not in meta.notes


def test_the_clock_offers_one_extension() -> None:
    timeline = clock_timeline(visa_type=VisaType.D12, entry_date=date(2027, 1, 1))
    assert timeline.extensions_possible == 1


def test_a_twelve_month_plan_is_covered() -> None:
    assert "Covers 360 days total." in _d12_reason(12)


def test_a_plan_beyond_the_total_is_still_too_long() -> None:
    assert "Max stay 360 days" in _d12_reason(13)
