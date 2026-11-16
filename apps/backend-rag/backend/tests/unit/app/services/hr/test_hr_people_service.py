"""HRPeopleService: roster flags and pulse over an in-memory fake connection."""

import sqlite3
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
                "avatar": "/static/team/contoh.jpg",
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
                "avatar": "data:image/png;base64,AAAA",
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
                "avatar": None,
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
async def test_roster_carries_the_portrait_path_of_a_local_photo_and_nothing_else():
    pool = FakePool()
    by = {r["team_member_id"]: r for r in await HRPeopleService(pool).roster(None)}
    assert by["a"]["avatar_url"] == "/static/team/contoh.jpg"
    assert by["b"]["avatar_url"] is None  # a data URI is not a portrait path
    assert by["c"]["avatar_url"] is None  # no avatar stored
    assert "tm.avatar" in pool.conn.calls[0][0]
    assert "avatar" not in {k for r in by.values() for k in r if k != "avatar_url"}


@pytest.mark.asyncio
async def test_a_scoped_roster_passes_the_emails_as_a_parameter_never_in_the_sql():
    pool = FakePool()
    await HRPeopleService(pool).roster(["member-e@example.test"])
    sql, args = pool.conn.calls[0]
    assert args == (["member-e@example.test"],) and "member-e@" not in sql


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


# ─── who counts as a person: the shared predicate, run for real over an in-memory table ───


def _visible(rows: list[tuple[str, str, str | None, bool, bool]]) -> set[str]:
    """rows: (id, email, role, active, has_hr_record). Runs the predicate text itself."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE team_members (id TEXT, email TEXT, role TEXT, active INT)")
    db.execute("CREATE TABLE hr_employees (id INTEGER, team_member_id TEXT)")
    for i, (tid, email, role, active, rec) in enumerate(rows):
        db.execute("INSERT INTO team_members VALUES (?,?,?,?)", (tid, email, role, int(active)))
        if rec:
            db.execute("INSERT INTO hr_employees VALUES (?,?)", (i + 1, tid))
    sql = mod.VISIBLE_PERSON_PREDICATE.replace("tm.active = true", "tm.active = 1")
    found = db.execute(
        "SELECT tm.id FROM team_members tm LEFT JOIN hr_employees e ON e.team_member_id = tm.id"
        f" WHERE {sql}"
    ).fetchall()
    return {r[0] for r in found}


def test_predicate_lists_employees_not_system_test_or_external_accounts():
    rows = [
        ("consultant", "staff-a@balizero.com", "consultant", True, False),
        ("consultant-upper", "Staff-B@BaliZero.com", "consultant", True, False),
        ("member-with-record", "staff-c@balizero.com", "member", True, True),
        ("e2e-member", "bot-a@balizero.com", "member", True, False),
        ("monitor", "probe-a@balizero.com", "monitoring", True, False),
        ("external", "advisor@example.com", "consultant", True, False),
        ("external-with-record", "d.person@example.com", "consultant", True, True),
        ("client-with-record", "client-a@balizero.com", "client", True, True),
        ("inactive", "staff-f@balizero.com", "consultant", False, True),
        ("no-role", "staff-g@balizero.com", None, True, False),
        ("no-role-with-record", "staff-h@balizero.com", None, True, True),
        # the non-person roles are excluded first, HR row or not, whatever their spelling
        ("monitor-with-record", "probe-b@balizero.com", "monitoring", True, True),
        ("monitor-upper", "probe-c@balizero.com", "Monitoring", True, False),
        ("partner", "partner-a@balizero.com", "partner", True, False),
        ("partner-with-record", "partner-b@balizero.com", "Partner", True, True),
        ("client-padded", "client-b@balizero.com", " client ", True, True),
        # group staff are INACTIVE rows that only this predicate opts in, by their card
        ("group-staff-with-card", "staff1@example.com", "group_staff", False, True),
        ("group-staff-no-card", "staff2@example.com", "group_staff", False, False),
        ("group-staff-active-with-card", "staff3@example.com", " Group_Staff ", True, True),
        ("former-employee-with-card", "former-a@balizero.com", "consultant", False, True),
    ]
    assert _visible(rows) == {
        "consultant",
        "consultant-upper",
        "member-with-record",
        "external-with-record",
        "no-role-with-record",
        "group-staff-with-card",
        "group-staff-active-with-card",
    }


def test_both_queries_use_the_one_predicate():
    assert mod.VISIBLE_PERSON_PREDICATE in mod._ROSTER_SQL
    assert mod.VISIBLE_PERSON_PREDICATE in mod._MEMBER_EMAIL_SQL
    assert "LEFT JOIN hr_employees e" in mod._MEMBER_EMAIL_SQL


def test_card_read_and_member_lock_use_the_same_group_staff_opt_in():
    from backend.app.services.hr.hr_service import HRService, _lock_visible_person_sql

    assert "IN ('group_staff')" in mod.VISIBLE_PERSON_PREDICATE
    assert mod.VISIBLE_PERSON_PREDICATE in _lock_visible_person_sql()
    assert mod.VISIBLE_PERSON_PREDICATE in mod._MEMBER_EMAIL_SQL
    # the HR card read must not drop an inactive group_staff row
    assert "IN ('group_staff')" in HRService._CARD_SELECT


def test_payroll_set_excludes_group_staff_whatever_on_payroll_says():
    from backend.app.services.hr.hr_service import payroll_set_sql

    sql = payroll_set_sql()
    assert "lower(trim(COALESCE(tm.role, ''))) NOT IN ('group_staff')" in sql
    # the People opt-in is still part of it; the explicit exclusion is what keeps them out
    assert mod.VISIBLE_PERSON_PREDICATE in sql
