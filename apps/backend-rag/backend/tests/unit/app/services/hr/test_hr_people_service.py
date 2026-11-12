"""HRPeopleService: roster flags and pulse over an in-memory fake connection."""

from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import pytest

from backend.app.services.hr import hr_people_service as mod
from backend.app.services.hr.hr_people_service import HRPeopleService, papers_missing
from backend.app.services.hr.hr_service import business_today

FULL = {
    "docs": dict.fromkeys(mod.DOC_SLOTS, "f"),
    "contract": {"file_id": "c"},
    "unit": "ops",
}


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        today = business_today()
        self.roster = [
            {
                "team_member_id": "a",
                "full_name": "Made Contoh",
                "role": "staff",
                "department": "setup",
                "employee_id": 1,
                "hire_date": date(2025, 1, 1),
                "employee_active": True,
                "profile": FULL,
            },
            {
                "team_member_id": "b",
                "full_name": "Wayan Tanpa",
                "role": "staff",
                "department": "tax",
                "employee_id": None,
                "hire_date": None,
                "employee_active": None,
                "profile": None,
            },
            {
                "team_member_id": "c",
                "full_name": "Ketut Lama",
                "role": "staff",
                "department": "tax",
                "employee_id": 3,
                "hire_date": date(2020, 1, 1),
                "employee_active": False,
                "profile": {"docs": {"ktp": "f"}},
            },
        ]
        self.today = today

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "FROM team_members tm" in sql:
            return self.roster
        if "hr_bonus_ledger" in sql:
            return [{"employee_id": 1, "n": 2}]
        if "status = 'approved'" in sql:
            assert args[1] == self.today
            return [{"employee_id": 3}]
        if "status = 'pending'" in sql:
            return [{"employee_id": 3, "n": 1}]
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append((sql, args))
        return {"bonus_n": 2, "bonus_idr": 10, "leave_n": 1, "payroll_status": "draft"}


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_papers_missing_counts_six_slots_and_tolerates_junk():
    assert papers_missing(FULL) == 0
    assert papers_missing({}) == 6
    assert papers_missing(None) == 6
    assert papers_missing({"docs": {"ktp": "f", "kk": ""}, "contract": "x"}) == 5
    assert papers_missing({"docs": [], "contract": {"file_id": "c"}}) == 5


@pytest.mark.asyncio
async def test_roster_flags_and_a_bounded_number_of_queries():
    pool = FakePool()
    rows = await HRPeopleService(pool).roster(None)
    by = {r["team_member_id"]: r for r in rows}
    assert by["a"]["flags"] == {
        "papers_missing": 0,
        "bonuses_pending": 2,
        "leave_pending": 0,
        "on_leave_today": False,
    }
    assert by["a"]["unit"] == "ops" and by["a"]["has_record"] is True
    assert by["b"]["has_record"] is False and by["b"]["flags"]["papers_missing"] is None
    assert by["b"]["employee_id"] is None
    assert by["c"]["flags"]["on_leave_today"] is True and by["c"]["flags"]["leave_pending"] == 1
    assert by["c"]["flags"]["papers_missing"] == 5 and by["c"]["active"] is False
    assert len(pool.conn.calls) == 4  # roster + three aggregates, whatever the roster size
    assert pool.conn.calls[0][1] == (None,)


@pytest.mark.asyncio
async def test_a_scoped_roster_passes_the_emails_as_a_parameter_never_in_the_sql():
    pool = FakePool()
    await HRPeopleService(pool).roster(["dea@balizero.com"])
    sql, args = pool.conn.calls[0]
    assert args == (["dea@balizero.com"],) and "dea@" not in sql


@pytest.mark.asyncio
async def test_pulse_numbers():
    pool = FakePool()
    svc = HRPeopleService(pool)
    roster = await svc.roster(None)
    pulse = await svc.pulse(roster)
    assert pulse["active_people"] == 2 and pulse["people_without_record"] == 1
    assert pulse["bonuses_pending_count"] == 2 and pulse["bonuses_pending_idr"] == 10
    assert pulse["leave_pending_count"] == 1
    assert pulse["payroll"]["status"] == "draft"
    assert pulse["payroll"]["month"] == business_today().month
