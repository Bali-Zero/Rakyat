"""My-overview aggregation and the service reads behind GET /api/hr/me/overview."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.hr import hr_service as hr_service_module
from backend.app.services.hr.hr_overview_logic import WITA, aggregate_attendance, bonus_totals
from backend.app.services.hr.hr_service import BUSINESS_TZ, HRService

UTC = timezone.utc


def _pool():
    conn = AsyncMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


def test_wita_matches_the_business_timezone():
    assert str(WITA) == BUSINESS_TZ == "Asia/Makassar"


# ─── bonus totals ────────────────────────────────────────────────────────


def _bonus(day: datetime, amount: int, status: str = "approved") -> dict:
    return {"awarded_at": day, "amount_idr": amount, "status": status}


def test_bonus_totals_exclude_rejected_reversed_and_cancelled():
    now = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
    rows = [
        _bonus(datetime(2026, 11, 3, 3, tzinfo=UTC), 100, "approved"),
        _bonus(datetime(2026, 11, 4, 3, tzinfo=UTC), 50, "paid"),
        _bonus(datetime(2026, 11, 5, 3, tzinfo=UTC), 10, "pending"),
        _bonus(datetime(2026, 11, 6, 3, tzinfo=UTC), 1000, "rejected"),
        _bonus(datetime(2026, 11, 7, 3, tzinfo=UTC), 2000, "reversed"),
        _bonus(datetime(2026, 11, 8, 3, tzinfo=UTC), 4000, "Cancelled"),
        _bonus(datetime(2026, 2, 1, 3, tzinfo=UTC), 7, "paid"),
    ]
    assert bonus_totals(rows, now) == {
        "month_total_idr": 160,
        "month_count": 3,
        "year_total_idr": 167,
    }


def test_bonus_month_is_cut_on_wita_not_utc():
    # 2026-10-31 17:00 UTC is 2026-11-01 01:00 WITA: a November bonus.
    now = datetime(2026, 11, 12, 3, tzinfo=UTC)
    rows = [_bonus(datetime(2026, 10, 31, 17, tzinfo=UTC), 5)]
    assert bonus_totals(rows, now)["month_total_idr"] == 5
    # ... and "now" itself is read in WITA: 2026-11-30 17:00 UTC is already 1 December.
    assert bonus_totals(rows, datetime(2026, 11, 30, 17, tzinfo=UTC))["month_total_idr"] == 0


def test_bonus_totals_of_nothing_are_zero():
    assert bonus_totals([], datetime(2026, 11, 12, tzinfo=UTC)) == {
        "month_total_idr": 0,
        "month_count": 0,
        "year_total_idr": 0,
    }


# ─── attendance ──────────────────────────────────────────────────────────

MONTHS = [(2026, 11), (2026, 10), (2026, 9)]


def _clock(d: date, hh: int, mm: int, hours: float | None) -> dict:
    return {
        "work_date": d,
        "clock_in_bali": datetime(d.year, d.month, d.day, hh, mm),  # naive = Bali wall time
        "hours_worked": hours,
    }


def test_a_month_with_on_time_and_late_days():
    clocks = [
        _clock(date(2026, 11, 2), 9, 0, 8.0),
        _clock(date(2026, 11, 3), 9, 20, 7.5),
        _clock(date(2026, 11, 4), 10, 10, 8.0),  # late: past 09:40
    ]
    late = [{"late_date": date(2026, 11, 4)}]
    out = aggregate_attendance(MONTHS, clocks, late)
    assert [m["month"] for m in out] == ["2026-11", "2026-10", "2026-09"]  # newest first
    assert out[0] == {
        "month": "2026-11",
        "days_present": 3,
        "hours": 23.5,
        "avg_clock_in": "09:30",  # (540 + 560 + 610) / 3 = 570 minutes
        "late_incidents": 1,
    }


def test_months_without_clock_ins_are_zero_days_with_null_hours_and_time():
    out = aggregate_attendance(MONTHS, [_clock(date(2026, 11, 2), 9, 0, 8.0)], [])
    assert out[1] == {
        "month": "2026-10",
        "days_present": 0,
        "hours": None,
        "avg_clock_in": None,
        "late_incidents": 0,
    }


def test_an_unreadable_incident_record_is_null_not_zero():
    out = aggregate_attendance(MONTHS, [_clock(date(2026, 11, 2), 9, 0, 8.0)], None)
    assert all(m["late_incidents"] is None for m in out)
    assert out[0]["days_present"] == 1


def test_aware_clock_in_is_read_in_bali_time_and_a_null_hours_row_still_counts():
    aware = {
        "work_date": date(2026, 11, 2),
        "clock_in_bali": datetime(2026, 11, 2, 1, 5, tzinfo=UTC),  # 09:05 WITA
        "hours_worked": None,
    }
    out = aggregate_attendance(MONTHS[:1], [aware], [])
    assert out[0]["days_present"] == 1
    assert out[0]["avg_clock_in"] == "09:05" and out[0]["hours"] is None


def test_two_rows_on_one_day_are_one_present_day():
    clocks = [_clock(date(2026, 11, 2), 9, 0, 4.0), _clock(date(2026, 11, 2), 13, 0, 4.0)]
    out = aggregate_attendance(MONTHS[:1], clocks, [])
    assert out[0]["days_present"] == 1 and out[0]["hours"] == 8.0


def test_nothing_recorded_gives_three_empty_months():
    out = aggregate_attendance(MONTHS, [], [])
    assert [m["days_present"] for m in out] == [0, 0, 0]


# ─── service reads ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_rows_reuse_the_own_data_reads_and_cap_the_lists():
    pool, conn = _pool()
    conn.fetch = AsyncMock(return_value=[])
    service = HRService(pool)
    slips = [{"id": i} for i in range(20)]
    reqs = [{"id": i} for i in range(40)]
    service.get_payslips = AsyncMock(return_value=slips)
    service.get_leave_balance = AsyncMock(return_value=[{"code": "annual"}])
    service.list_leave_requests = AsyncMock(return_value=reqs)
    with patch.object(hr_service_module, "business_today", return_value=date(2026, 11, 12)):
        rows = await service.get_my_overview_rows(7, "tm-fake-7")
    service.get_payslips.assert_awaited_once_with(
        7
    )  # the non-admin payslip rule: own employee only
    service.get_leave_balance.assert_awaited_once_with(7, 2026)
    service.list_leave_requests.assert_awaited_once_with(7)
    assert len(rows["payslips"]) == 12 and len(rows["requests"]) == 20
    assert rows["balances"] == [{"code": "annual"}]


@pytest.mark.asyncio
async def test_bonus_and_attendance_sql_are_parameterised_wita_and_keyed_by_member():
    pool, conn = _pool()
    seen: list[tuple] = []

    async def fetch(sql, *args):
        seen.append((sql, args))
        return []

    conn.fetch = fetch
    service = HRService(pool)
    service.get_payslips = AsyncMock(return_value=[])
    service.get_leave_balance = AsyncMock(return_value=[])
    service.list_leave_requests = AsyncMock(return_value=[])
    with patch.object(hr_service_module, "business_today", return_value=date(2026, 1, 15)):
        rows = await service.get_my_overview_rows(7, "tm-fake-7")
    bonus, clock, late = seen
    assert "employee_id = $1" in bonus[0] and "AT TIME ZONE 'Asia/Makassar'" in bonus[0]
    assert bonus[1] == (7, 2026)
    assert "user_id = $1" in clock[0] and "daily_work_hours" in clock[0]
    # January: the window reaches back over Nov + Dec of the previous year, ends before February.
    assert clock[1] == ("tm-fake-7", date(2025, 11, 1), date(2026, 2, 1))
    assert "tm.id = $1" in late[0] and late[1] == ("tm-fake-7", date(2025, 11, 1), date(2026, 2, 1))
    assert [m["month"] for m in rows["attendance"]] == ["2026-01", "2025-12", "2025-11"]
    for sql, _ in seen:
        assert "tm-fake-7" not in sql


@pytest.mark.asyncio
async def test_each_attendance_source_degrades_on_its_own():
    pool, conn = _pool()
    calls = {"n": 0}

    async def fetch(sql, *args):
        calls["n"] += 1
        if "attendance_late_incidents" in sql:
            raise RuntimeError("relation does not exist")
        return [
            {
                "work_date": date(2026, 11, 2),
                "clock_in_bali": datetime(2026, 11, 2, 9, 0),
                "hours_worked": 8.0,
            }
        ]

    conn.fetch = fetch
    out = await HRService(pool)._my_attendance("tm-fake-7", date(2026, 11, 12))
    assert out[0]["days_present"] == 1 and out[0]["late_incidents"] is None

    async def broken(sql, *args):
        raise RuntimeError("permission denied")

    conn.fetch = broken
    assert await HRService(pool)._my_attendance("tm-fake-7", date(2026, 11, 12)) == []


def test_window_end_is_exclusive_first_of_next_month_in_december():
    # Guards the year rollover in the window arithmetic.
    start_end = {}
    pool, conn = _pool()

    async def fetch(sql, *args):
        start_end["a"] = args[1:]
        return []

    conn.fetch = fetch
    import asyncio

    asyncio.run(HRService(pool)._my_attendance("tm", date(2026, 12, 5)))
    assert start_end["a"] == (date(2026, 10, 1), date(2027, 1, 1))
