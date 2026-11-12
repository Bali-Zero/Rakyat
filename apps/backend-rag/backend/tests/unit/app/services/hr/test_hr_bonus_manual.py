"""Manual bonuses, reject/delete, practice-type dropdown, department chips (synthetic data)."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.hr.hr_service import (
    BonusPeriodLockedError,
    HRService,
    PayrollBonusesChangedError,
    department_of,
    practice_group,
)


@pytest.fixture
def pool():
    p = MagicMock()
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    p.acquire = MagicMock(return_value=acq)
    p._conn = conn
    return p


@pytest.fixture
def svc(pool):
    return HRService(db_pool=pool)


def _args(**over):
    base = {
        "employee_id": 7,
        "amount_idr": 250_000,
        "reason": "  Extra filing weekend  ",
        "practice_type_code": None,
        "awarded_on": date(2026, 11, 10),
        "created_by": "fake.admin@balizero.com",
    }
    base.update(over)
    return base


class TestCreateManualBonus:
    @pytest.mark.asyncio
    async def test_inserts_pending_manual_row_at_noon_wita(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(side_effect=[{"id": 7}, {"id": 99, "source": "manual"}])
        conn.fetchval = AsyncMock(return_value=None)  # period not locked
        row = await svc.create_manual_bonus(**_args())
        assert row == {"id": 99, "source": "manual"}
        sql, *params = conn.fetchrow.await_args_list[1].args
        assert "'manual'" in sql and "'pending'" in sql and "practice_id" not in sql
        assert params[0] == 7
        assert params[1] == "manual"  # no service named: the NOT NULL sentinel
        assert params[2] == 250_000
        awarded_at = params[3]
        assert awarded_at.utcoffset() == timedelta(hours=8)
        assert (awarded_at.year, awarded_at.month, awarded_at.day, awarded_at.hour) == (
            2026,
            11,
            10,
            12,
        )
        assert params[4] == "Extra filing weekend"  # trimmed
        assert params[5] == "fake.admin@balizero.com"

    @pytest.mark.asyncio
    async def test_named_service_is_stored_as_the_code(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(side_effect=[{"id": 7}, {"id": 1}])
        conn.fetchval = AsyncMock(side_effect=[1, None])  # service known, month open
        await svc.create_manual_bonus(**_args(practice_type_code="tax_fake_service"))
        assert conn.fetchrow.await_args_list[1].args[2] == "tax_fake_service"

    @pytest.mark.asyncio
    async def test_unknown_employee_returns_none_and_inserts_nothing(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(return_value=None)
        assert await svc.create_manual_bonus(**_args()) is None
        assert conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_service_is_a_value_error(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(return_value={"id": 7})
        conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(ValueError):
            await svc.create_manual_bonus(**_args(practice_type_code="nope"))
        assert conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    async def test_locked_month_raises_and_inserts_nothing(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(return_value={"id": 7})
        conn.fetchval = AsyncMock(return_value="approved")  # the period's status
        with pytest.raises(BonusPeriodLockedError):
            await svc.create_manual_bonus(**_args())
        assert conn.fetchrow.await_count == 1
        lock_sql, month, year = conn.fetchval.await_args.args
        assert "FOR SHARE" in lock_sql and (month, year) == (11, 2026)


class TestRejectAndDelete:
    @pytest.mark.asyncio
    async def test_reject_pending(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(
            return_value={"id": 3, "status": "rejected", "_existed": True}
        )
        row = await svc.reject_bonus(3, "tm-1", "duplicate")
        assert row == {"id": 3, "status": "rejected"}
        sql = pool._conn.fetchrow.await_args.args[0]
        assert "status = 'pending'" in sql and sql.count("hr_bonus_ledger") == 2  # one statement

    @pytest.mark.asyncio
    async def test_reject_unknown_id_is_lookup_error(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(return_value={"id": None, "_existed": False})
        with pytest.raises(LookupError):
            await svc.reject_bonus(3, "tm-1")

    @pytest.mark.asyncio
    async def test_reject_existing_not_pending_is_value_error(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(return_value={"id": None, "_existed": True})
        with pytest.raises(ValueError):
            await svc.reject_bonus(3, "tm-1")

    @pytest.mark.asyncio
    async def test_delete_is_restricted_to_pending_manual_rows(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(return_value={"deleted_id": 5, "existed": True})
        assert await svc.delete_manual_bonus(5) == "deleted"
        sql = pool._conn.fetchrow.await_args.args[0]
        assert "source = 'manual'" in sql and "status = 'pending'" in sql

    @pytest.mark.asyncio
    async def test_delete_of_another_row_is_a_conflict(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(return_value={"deleted_id": None, "existed": True})
        assert await svc.delete_manual_bonus(5) == "conflict"

    @pytest.mark.asyncio
    async def test_delete_of_an_unknown_id_is_missing(self, svc, pool):
        pool._conn.fetchrow = AsyncMock(return_value={"deleted_id": None, "existed": False})
        assert await svc.delete_manual_bonus(5) == "missing"


class TestPracticeTypes:
    @pytest.mark.asyncio
    async def test_grouped_ordered_and_tax_included(self, svc, pool):
        pool._conn.fetch = AsyncMock(
            return_value=[
                {"code": "tax_fake_report", "name": "Annual report", "category": "tax"},
                {"code": "zzz_misc", "name": "Misc", "category": None},
                {"code": "company_fake_pt", "name": "Company set up", "category": "company"},
                {"code": "kitas_fake", "name": "Work permit", "category": "kitas"},
                {"code": "visa_fake", "name": "Alpha visa", "category": "visa"},
            ]
        )
        out = await svc.list_bonus_practice_types()
        assert [t["group"] for t in out] == ["visa", "visa", "company", "tax", "other"]
        assert [t["name"] for t in out][:2] == ["Alpha visa", "Work permit"]
        assert any(t["code"].startswith("tax") for t in out)
        assert "is_active = TRUE" in pool._conn.fetch.await_args.args[0]


def test_practice_group_rules():
    assert practice_group("tax_vat") == "tax"
    assert practice_group("x", "tax") == "tax"
    assert practice_group("company_pt_pma") == "company"
    assert practice_group("kitap_application") == "visa"
    assert practice_group("voa_extension") == "visa"
    assert practice_group("sertifikat_x") == "other"


def test_department_of_uses_the_org_chart_then_the_stored_text():
    assert department_of("Angel.Tax@balizero.com") == "tax"
    assert department_of("adit@balizero.com") == "setup"
    assert department_of("fake.person@balizero.com", "Tax") == "tax"
    assert department_of("fake.person@balizero.com", "ops") == "office"
    assert department_of(None) == "office"


class TestListingAndTotals:
    @pytest.mark.asyncio
    async def test_list_bonuses_left_joins_practices_and_adds_department(self, svc, pool):
        pool._conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "source": "manual",
                    "reason": "r",
                    "employee_email": "kadek.tax@balizero.com",
                    "employee_department": None,
                    "awarded_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
                }
            ]
        )
        rows = await svc.list_bonuses()
        sql = pool._conn.fetch.await_args.args[0]
        assert "LEFT JOIN practices p" in sql and "JOIN practices p" not in sql.replace(
            "LEFT JOIN practices p", ""
        )
        assert rows[0]["employee_department"] == "tax"

    @pytest.mark.asyncio
    async def test_my_dashboard_and_summary_exclude_rejected_and_reversed(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(side_effect=[{"count": 0, "total": 0}, None])
        conn.fetch = AsyncMock(return_value=[])
        await svc.get_my_dashboard(1)
        assert (
            "NOT IN ('rejected', 'reversed', 'cancelled')"
            in conn.fetchrow.await_args_list[0].args[0]
        )

        conn.fetchrow = AsyncMock(return_value={"bonus_count": 0})
        await svc.get_bonus_summary(1, 11, 2026)
        assert "NOT IN ('rejected', 'reversed', 'cancelled')" in conn.fetchrow.await_args.args[0]


class TestPeriodLocks:
    @pytest.mark.asyncio
    async def test_approve_bonus_in_a_locked_month_raises(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(return_value={"status": "pending", "m": 11, "y": 2026})
        conn.fetchval = AsyncMock(return_value="paid")
        with pytest.raises(BonusPeriodLockedError):
            await svc.approve_bonus(3, "tm-1")
        assert conn.fetchrow.await_count == 1  # never reached the UPDATE
        assert "FOR UPDATE" in conn.fetchrow.await_args.args[0]

    @pytest.mark.asyncio
    async def test_approve_bonus_unknown_or_not_pending_keeps_the_value_error(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(ValueError):
            await svc.approve_bonus(3, "tm-1")
        conn.fetchrow = AsyncMock(return_value={"status": "approved", "m": 1, "y": 2026})
        with pytest.raises(ValueError):
            await svc.approve_bonus(3, "tm-1")

    @pytest.mark.asyncio
    async def test_approve_payroll_refuses_when_bonuses_drifted(self, svc, pool):
        conn = pool._conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "status": "calculated",
                "payroll_month": 11,
                "payroll_year": 2026,
            }
        )
        conn.fetchval = AsyncMock(return_value=42)  # an employee whose bonus total differs
        with pytest.raises(PayrollBonusesChangedError, match="recalculate"):
            await svc.approve_payroll(1, "tm-1")
        assert conn.fetchrow.await_count == 1  # the UPDATE never ran
