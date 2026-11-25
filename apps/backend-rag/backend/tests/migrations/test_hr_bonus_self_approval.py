"""A bonus needs an approver other than the person it is for, against a real Postgres.

Own schema `hrselfappr`: migration_068's DDL and triggers, then migrations 354 and 357. The
service refuses inside the transaction that holds the bonus row; the route maps that to a 400
that names no person. All names are placeholders.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr import hr_service
from backend.app.services.hr.hr_service import HRService
from backend.db.migration_base import split_migration_sql
from backend.migrations.migration_068_hr_payroll import UPGRADE_SQL
from backend.tests.migrations.hr_bonus_fixture import install_delivery_payroll_migrations

pytestmark = pytest.mark.integration

SCHEMA = "hrselfappr"
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
AWARDED = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
NEUTRAL = "A bonus needs an approver other than the person it is for"


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@dataclass
class Env:
    svc: HRService
    pool: asyncpg.Pool
    emp_a: int  # "tm-a" has an hr_employees row and is the beneficiary of the test bonuses
    emp_b: int  # "tm-b" has an hr_employees row too and may approve for tm-a


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    admin = await asyncpg.connect(_dsn())
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('hr-bonus-self-approval-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await admin.execute(
            f"SET search_path TO {SCHEMA};"
            "CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT,"
            " role TEXT, active BOOLEAN NOT NULL DEFAULT TRUE, avatar TEXT);"
            "CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);"
        )
        await admin.execute(UPGRADE_SQL)
        for name in ("354_hr_employees_profile.sql", "357_hr_bonus_manual.sql"):
            await admin.execute(
                split_migration_sql((MIGRATIONS / name).read_text(encoding="utf-8"))[0]
            )
        await install_delivery_payroll_migrations(admin)
        await admin.execute(
            "INSERT INTO team_members (id, full_name, email, role) VALUES "
            "('tm-a','Fake A','fake.a@example.com','staff'),"
            "('tm-b','Fake B','fake.b@example.com','staff'),"
            "('tm-ext','Fake Ext','fake.ext@example.com','admin');"
            "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr) "
            "VALUES ('tm-a','2025-01-01',5000000),('tm-b','2025-01-01',5000000)"
        )
        emp_a = await admin.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-a'")
        emp_b = await admin.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-b'")
        pool = await asyncpg.create_pool(
            _dsn(),
            server_settings={"search_path": SCHEMA},
            statement_cache_size=0,
            min_size=1,
            max_size=4,
        )
        yield Env(HRService(pool), pool, emp_a, emp_b)
    finally:
        if pool:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('hr-bonus-self-approval-test'))")
        await admin.close()


async def _pending_bonus(pool: asyncpg.Pool, employee_id: int) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, "
            "awarded_at, source, reason) VALUES ($1,'manual',100000,'pending',$2,'manual','why') "
            "RETURNING id",
            employee_id,
            AWARDED,
        )


async def _state(pool: asyncpg.Pool, bonus_id: int) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT status, approved_by, approved_at FROM hr_bonus_ledger WHERE id=$1", bonus_id
        )


@pytest.mark.asyncio
async def test_the_beneficiary_cannot_approve_their_own_pending_bonus(env: Env) -> None:
    bonus_id = await _pending_bonus(env.pool, env.emp_a)

    with pytest.raises(ValueError, match="other than the person it is for") as refused:
        await env.svc.approve_bonus(bonus_id, "tm-a")

    assert isinstance(refused.value, hr_service.SelfApprovalError)
    row = await _state(env.pool, bonus_id)
    assert row["status"] == "pending"
    assert row["approved_by"] is None and row["approved_at"] is None


@pytest.mark.asyncio
async def test_a_different_approver_with_an_hr_card_approves_it(env: Env) -> None:
    bonus_id = await _pending_bonus(env.pool, env.emp_a)

    row = await env.svc.approve_bonus(bonus_id, "tm-b")

    assert row["status"] == "approved" and row["approved_by"] == "tm-b"
    stored = await _state(env.pool, bonus_id)
    assert stored["status"] == "approved" and stored["approved_by"] == "tm-b"
    assert stored["approved_at"] is not None


@pytest.mark.asyncio
async def test_an_approver_with_no_hr_card_approves_it(env: Env) -> None:
    bonus_id = await _pending_bonus(env.pool, env.emp_a)

    row = await env.svc.approve_bonus(bonus_id, "tm-ext")

    assert row["status"] == "approved" and row["approved_by"] == "tm-ext"


@pytest.mark.asyncio
async def test_the_refusal_follows_the_beneficiary_not_the_approver(env: Env) -> None:
    # tm-b has a card and may approve tm-a's bonus, but not a bonus that is for tm-b.
    for_a = await _pending_bonus(env.pool, env.emp_a)
    for_b = await _pending_bonus(env.pool, env.emp_b)

    assert (await env.svc.approve_bonus(for_a, "tm-b"))["status"] == "approved"
    with pytest.raises(ValueError, match="other than the person it is for") as refused:
        await env.svc.approve_bonus(for_b, "tm-b")
    assert isinstance(refused.value, hr_service.SelfApprovalError)
    assert (await _state(env.pool, for_b))["status"] == "pending"
    # and once refused, the other person is still free to approve it
    assert (await env.svc.approve_bonus(for_b, "tm-a"))["approved_by"] == "tm-a"


@pytest.mark.asyncio
async def test_the_approve_route_answers_400_for_self_approval_and_names_no_one(
    env: Env,
) -> None:
    bonus_id = await _pending_bonus(env.pool, env.emp_a)

    def client_for(user_id: str) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        session = {"user_id": user_id, "email": f"{user_id}@example.com", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: session
        app.dependency_overrides[get_database_pool] = lambda: env.pool
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    with patch.object(hr_router, "invalidate_cache", new=AsyncMock()):
        async with client_for("tm-a") as c:
            res = await c.post(f"/api/hr/bonuses/{bonus_id}/approve")
        assert res.status_code == 400
        assert res.json() == {"detail": NEUTRAL}
        assert "tm-a" not in res.text and str(bonus_id) not in res.text
        assert str(env.emp_a) not in res.text
        assert (await _state(env.pool, bonus_id))["status"] == "pending"

        # the same bonus, the same route: only the approver differs
        async with client_for("tm-ext") as c:
            ok = await c.post(f"/api/hr/bonuses/{bonus_id}/approve")
        assert ok.status_code == 200
        assert ok.json()["bonus"]["approved_by"] == "tm-ext"
