"""Outcome endpoint refuses alerts that are already closed."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.deps.auth import get_current_user
from backend.app.deps.database import get_database_pool
from backend.app.routers.compliance_alerts import router

pytestmark = pytest.mark.integration
_ADMIN_USER = {"email": "admin.one@example.test", "role": "admin"}
_DEFAULT_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
)


def make_app(user: dict, pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return user

    async def fake_pool():
        return pool

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_database_pool] = fake_pool
    return app


@pytest.fixture(autouse=True)
def _synthetic_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    # The router recognises an admin by address: the test names its own.
    monkeypatch.setattr(
        "backend.app.routers.compliance_alerts._ADMIN_EMAILS",
        frozenset({_ADMIN_USER["email"]}),
    )


@pytest_asyncio.fixture(scope="function")
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    try:
        pool = await asyncpg.create_pool(_DEFAULT_DB_URL, min_size=1, max_size=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"DB unreachable: {exc}")
        return

    skip_reason: str | None = None
    try:
        async with pool.acquire() as conn:
            for table in ("clients", "compliance_alerts", "alert_outcomes"):
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=$1)",
                    table,
                )
                if not exists:
                    skip_reason = f"required table '{table}' missing in test DB"
                    break
        if skip_reason is None:
            yield pool
    finally:
        await pool.close()
    if skip_reason:
        pytest.skip(skip_reason)


async def _insert_client(pool: asyncpg.Pool, email: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO clients (full_name, email, created_at, updated_at) "
            "VALUES ($1,$2,NOW(),NOW()) RETURNING id",
            "Closed Alert Test Client",
            email,
        )
    return dict(row)


async def _insert_alert(pool: asyncpg.Pool, client_id: int, *, status: str = "pending") -> dict:
    alert_id = f"closed_{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO compliance_alerts ("
            "alert_id, client_id, category, severity, status, deadline, days_until, dedup_key"
            ") VALUES ($1,$2,'visa_expiry','urgent',$3,$4,7,$5)",
            alert_id,
            client_id,
            status,
            date.today() + timedelta(days=7),
            f"visa_expiry:{client_id}:{alert_id}",
        )
    return {"alert_id": alert_id, "client_id": client_id}


async def _delete_alert(pool: asyncpg.Pool, alert_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_outcomes WHERE alert_id = $1", alert_id)
        await conn.execute("DELETE FROM compliance_alerts WHERE alert_id = $1", alert_id)


async def _delete_client(pool: asyncpg.Pool, client_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM clients WHERE id = $1", client_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_status", ["resolved", "expired"])
async def test_post_outcome_refuses_closed_alert_without_writing(
    db_pool: asyncpg.Pool, closed_status: str
) -> None:
    """Closed alerts keep their status and have no recorded outcome."""
    client = await _insert_client(db_pool, f"closed-{closed_status}@example.com")
    alert = await _insert_alert(db_pool, client["id"], status=closed_status)
    try:
        app = make_app(_ADMIN_USER, db_pool)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post(
                f"/api/compliance/alerts/{alert['alert_id']}/outcome",
                json={"outcome": "acted"},
            )

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == f"alert is already closed: {closed_status}"
        async with db_pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM compliance_alerts WHERE alert_id = $1", alert["alert_id"]
            )
            outcome_count = await conn.fetchval(
                "SELECT COUNT(*) FROM alert_outcomes WHERE alert_id = $1", alert["alert_id"]
            )
        assert status == closed_status
        assert outcome_count == 0
    finally:
        await _delete_alert(db_pool, alert["alert_id"])
        await _delete_client(db_pool, client["id"])


@pytest.mark.asyncio
async def test_post_outcome_records_open_alert_with_existing_response_shape(
    db_pool: asyncpg.Pool,
) -> None:
    """Open alerts still accept outcomes with the existing response fields."""
    client = await _insert_client(db_pool, "open-alert@example.com")
    alert = await _insert_alert(db_pool, client["id"])
    try:
        app = make_app(_ADMIN_USER, db_pool)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post(
                f"/api/compliance/alerts/{alert['alert_id']}/outcome",
                json={"outcome": "dismissed"},
            )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "alert_id": alert["alert_id"],
            "outcome": "dismissed",
            "status": "acknowledged",
        }
    finally:
        await _delete_alert(db_pool, alert["alert_id"])
        await _delete_client(db_pool, client["id"])


@pytest.mark.asyncio
async def test_post_outcome_keeps_missing_alert_response(db_pool: asyncpg.Pool) -> None:
    """A missing alert retains the endpoint's established not-found response."""
    app = make_app(_ADMIN_USER, db_pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/compliance/alerts/missing-alert/outcome",
            json={"outcome": "acted"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "alert not found"}
