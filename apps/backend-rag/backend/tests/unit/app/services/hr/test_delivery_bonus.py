"""Regression coverage for HR qualification, WITA cutoff and delayed rate resolution."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr.hr_service import BonusPeriodLockedError


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-11-28T15:59:59+00:00", date(2026, 11, 28)),
        ("2026-11-28T16:00:00+00:00", date(2026, 11, 29)),
        ("2026-11-30T15:59:59+00:00", date(2026, 11, 30)),
        ("2026-11-30T16:00:00+00:00", date(2026, 12, 1)),
        ("2026-12-31T15:59:59+00:00", date(2026, 12, 31)),
        ("2026-12-31T16:00:00+00:00", date(2027, 1, 1)),
        ("2027-02-28T15:59:59+00:00", date(2027, 2, 28)),
        ("2027-02-28T16:00:00+00:00", date(2027, 3, 1)),
    ],
)
def test_cutoff_uses_the_employee_business_day(instant, expected):
    assert bonus.payroll_award_date(datetime.fromisoformat(instant)).date() == expected


@pytest.mark.parametrize("day", range(1, 31))
def test_every_november_day_awards_in_november(day):
    # The bonus month is the calendar month up to its last day: no roll-over after the 28th.
    late_evening = datetime(2026, 11, day, 23, 59, 59, tzinfo=bonus.WITA)
    awarded = bonus.payroll_award_date(late_evening)
    assert (awarded.year, awarded.month, awarded.day) == (2026, 11, day)


@pytest.mark.parametrize(
    ("receipt", "expected_writes"),
    [
        ({"employee_id": 1, "amount_idr": 100_000, "ledger_id": None}, 1),
        # A rate of 0 means the service carries no bonus: never a payroll line.
        ({"employee_id": 1, "amount_idr": 0, "ledger_id": None}, 0),
        ({"employee_id": 1, "amount_idr": None, "ledger_id": None}, 0),
        ({"employee_id": None, "amount_idr": 100_000, "ledger_id": None}, 0),
        ({"employee_id": 1, "amount_idr": 100_000, "ledger_id": 22}, 0),
        (None, 0),
    ],
)
@pytest.mark.asyncio
async def test_only_new_eligible_positively_priced_event_writes_payroll(
    monkeypatch, receipt, expected_writes
):
    at = datetime.now(timezone.utc)
    if receipt:
        receipt = {**receipt, "awarded_at": at}
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        None,  # the plain read: no receipt yet
        receipt,
        {"event_kind": "electronic_visa_issued", "status": "pending"},
    ]
    write = AsyncMock()
    monkeypatch.setattr(bonus, "_write_ledger", write)
    result = await bonus.record_delivery_bonus(
        conn,
        12,
        "visa_c1_tourism",
        "member@example.invalid",
        "electronic_visa_issued",
        issued_on=date(2020, 12, 29),
    )
    assert result["event_kind"] == "electronic_visa_issued"
    assert write.await_count == expected_writes
    insert = conn.fetchrow.await_args_list[1].args
    assert insert[5].date() == datetime.now(bonus.WITA).date()
    assert insert[6] == bonus.payroll_award_date(insert[5])
    assert insert[7] == insert[5].date()
    assert insert[8] == date(2020, 12, 29)
    assert insert[9] == insert[5]  # recorded_at, which earned_at replaces only when given


@pytest.mark.asyncio
async def test_an_existing_receipt_is_returned_without_an_insert_or_a_ledger_attempt(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.return_value = {"event_kind": "service_delivered", "status": "awaiting_employee"}
    write = AsyncMock()
    monkeypatch.setattr(bonus, "_write_ledger", write)
    result = await bonus.record_delivery_bonus(
        conn,
        12,
        "company_pt_pma",
        "member@example.invalid",
        "service_delivered",
        earned_at=datetime(2026, 11, 30, 3, tzinfo=timezone.utc),
    )
    assert result["status"] == "awaiting_employee"
    assert conn.fetchrow.await_count == 1  # the plain read only
    assert "INSERT" not in conn.fetchrow.await_args.args[0]
    conn.execute.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_earned_at_dates_the_receipt_its_month_and_its_rate_but_not_recorded_at(monkeypatch):
    earned = datetime(2026, 11, 30, 15, 30, tzinfo=timezone.utc)  # 2026-11-30 23:30 in Bali
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None, {"event_kind": "service_delivered", "status": "x"}]
    monkeypatch.setattr(bonus, "_write_ledger", AsyncMock())
    await bonus.record_delivery_bonus(
        conn, 12, "company_pt_pma", "m@example.invalid", "service_delivered", earned_at=earned
    )
    insert = conn.fetchrow.await_args_list[1].args
    assert insert[5] == earned
    assert insert[6] == bonus.payroll_award_date(earned) and insert[6].month == 11
    assert insert[7] == date(2026, 11, 30)
    assert insert[9] != earned and insert[9].date() >= date(2026, 11, 28)


@pytest.mark.asyncio
async def test_closed_payroll_does_not_cancel_the_qualifying_milestone(monkeypatch):
    receipt = {
        "employee_id": 1,
        "amount_idr": 100,
        "ledger_id": None,
        "awarded_at": datetime.now(timezone.utc),
    }
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, receipt, {"status": "payroll_locked"}]
    monkeypatch.setattr(bonus, "_write_ledger", AsyncMock(side_effect=BonusPeriodLockedError))
    assert (
        await bonus.record_delivery_bonus(conn, 2, "tax", "a@example.invalid", "service_delivered")
    )["status"] == "payroll_locked"


@pytest.mark.asyncio
async def test_hr_replay_is_idempotent_and_preserves_fixed_rate():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [{"ledger_id": 12}, {"status": "pending"}]
    assert (await bonus.resolve_delivery_bonus(conn, 5, 10, "reviewer"))["status"] == "pending"
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "ledger_id": None,
        "amount_idr": 123,
        "payroll_ineligible": False,
        "forfeited": False,
    }
    with pytest.raises(ValueError, match="recorded rate"):
        await bonus.resolve_delivery_bonus(conn, 5, 456, "reviewer")


@pytest.mark.asyncio
async def test_desk_does_not_query_payroll_for_ineligible_account():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    result = await bonus.delivery_desk(conn, "member-a")
    assert not result["on_payroll"] and "totals" not in result
    assert conn.fetchrow.await_count == 1 and not conn.fetch.await_count


@pytest.mark.asyncio
async def test_resolving_missing_rate_requires_current_payroll_eligibility():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "ledger_id": None,
        "amount_idr": None,
        "employee_id": 1,
        "payroll_ineligible": False,
        "forfeited": False,
        "original_team_member_id": "member-a",
    }
    conn.fetchval.return_value = False
    with pytest.raises(ValueError, match="no longer on payroll"):
        await bonus.resolve_delivery_bonus(conn, 2, 175000, "hr-reviewer")
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_visa_needs_real_issue_date_but_a_replacement_preserves_its_receipt():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with pytest.raises(ValueError, match="actual issue date"):
        await bonus.record_delivery_bonus(
            conn, 8, "visa", "member@example.invalid", "electronic_visa_issued"
        )
    with pytest.raises(ValueError, match="actual issue date"):
        await bonus.record_delivery_bonus(
            conn, 8, "visa", "member@example.invalid", "electronic_visa_issued", issued_on=date.max
        )
    conn.fetchrow.return_value = {
        "status": "awaiting_employee",
        "event_kind": "electronic_visa_issued",
    }
    assert (
        await bonus.record_delivery_bonus(
            conn, 8, "visa", "replacement@example.invalid", "electronic_visa_issued"
        )
    )["status"] == "awaiting_employee"


@pytest.mark.asyncio
async def test_known_off_payroll_claim_cannot_be_assigned_even_to_an_eligible_replacement():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"ledger_id": None, "payroll_ineligible": True}
    with pytest.raises(ValueError, match="outside payroll at delivery"):
        await bonus.resolve_delivery_bonus(conn, 2, 175000, "hr-reviewer", employee_id=2)
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolving_with_a_zero_amount_writes_no_ledger_row(monkeypatch):
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {
            "ledger_id": None,
            "amount_idr": None,
            "employee_id": 1,
            "payroll_ineligible": False,
            "forfeited": False,
            "original_team_member_id": "member-a",
            "awarded_at": datetime.now(timezone.utc),
        },
        {"ledger_id": None, "amount_idr": 0, "employee_id": 1},
        {"event_kind": "service_delivered", "status": "no_bonus"},
    ]
    conn.fetchval.return_value = True
    write = AsyncMock()
    monkeypatch.setattr(bonus, "_write_ledger", write)
    result = await bonus.resolve_delivery_bonus(conn, 2, 0, "hr-reviewer")
    assert result["status"] == "no_bonus"
    write.assert_not_awaited()
