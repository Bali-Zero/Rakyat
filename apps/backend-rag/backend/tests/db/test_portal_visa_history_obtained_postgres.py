"""Real-Postgres tests: the portal Visa History lists only permits obtained.

`get_visa_status` used to list every client-visible visa practice and map
anything not "completed" to "expired", so an application still in progress or a
cancelled one reached the client as an "expired" permit. Only practices whose
status means the permit was obtained belong in the history. Synthetic data only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg
import pytest

from backend.services.portal.portal_service import PortalService

pytestmark = pytest.mark.integration


class _SingleConnectionPool:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner) -> asyncpg.Connection:
                return conn

            async def __aexit__(self_inner, *exc: object) -> bool:
                return False

        return _Ctx()


async def _ensure_schema_current(conn: asyncpg.Connection) -> None:
    await conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS issue_date date")
    await conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS ocr_extracted_data jsonb")
    await conn.execute("ALTER TABLE practices ADD COLUMN IF NOT EXISTS client_visible boolean")


async def _make_client(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('W-portal-visa-history test client') RETURNING id",
    )


async def _make_type(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM practice_types WHERE code = 'PW10_KITAS' AND category = 'visa'",
    )
    if row:
        return row["id"]
    return await conn.fetchval(
        "INSERT INTO practice_types (code, name, category) "
        "VALUES ('PW10_KITAS', 'Synthetic KITAS', 'visa') RETURNING id",
    )


async def _make_practice(
    conn: asyncpg.Connection,
    *,
    client_id: int,
    type_id: int,
    status: str,
    start: date,
    client_visible: bool | None = True,
) -> int:
    return await conn.fetchval(
        "INSERT INTO practices (client_id, practice_type_id, status, start_date, client_visible) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        client_id,
        type_id,
        status,
        start,
        client_visible,
    )


async def _history(conn: asyncpg.Connection, client_id: int) -> list[dict[str, Any]]:
    service = PortalService(_SingleConnectionPool(conn))  # type: ignore[arg-type]
    result = await service.get_visa_status(client_id, current_user={"client_id": client_id})
    return result["history"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "not_obtained",
    ["on_process", "inquiry", "cancelled", "waiting_documents", "in_progress"],
)
async def test_history_drops_a_practice_that_was_not_obtained(
    db_tx: asyncpg.Connection, not_obtained: str
) -> None:
    await _ensure_schema_current(db_tx)
    client_id = await _make_client(db_tx)
    type_id = await _make_type(db_tx)
    done = await _make_practice(
        db_tx, client_id=client_id, type_id=type_id, status="completed", start=date(2024, 1, 10)
    )
    await _make_practice(
        db_tx, client_id=client_id, type_id=type_id, status=not_obtained, start=date(2025, 3, 5)
    )

    history = await _history(db_tx, client_id)

    assert [h["id"] for h in history] == [str(done)]
    assert history[0]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["expiring_soon", "pending_renewal"])
async def test_history_shows_a_live_permit_as_completed_not_expired(
    db_tx: asyncpg.Connection, live_status: str
) -> None:
    await _ensure_schema_current(db_tx)
    client_id = await _make_client(db_tx)
    type_id = await _make_type(db_tx)
    pid = await _make_practice(
        db_tx, client_id=client_id, type_id=type_id, status=live_status, start=date(2024, 6, 1)
    )

    history = await _history(db_tx, client_id)

    assert history == [
        {
            "id": str(pid),
            "type": history[0]["type"],
            "period": history[0]["period"],
            "status": "completed",
        }
    ]


@pytest.mark.asyncio
async def test_history_keeps_completed_and_expired_hides_invisible_and_orders_desc(
    db_tx: asyncpg.Connection,
) -> None:
    await _ensure_schema_current(db_tx)
    client_id = await _make_client(db_tx)
    type_id = await _make_type(db_tx)
    old_expired = await _make_practice(
        db_tx, client_id=client_id, type_id=type_id, status="expired", start=date(2021, 2, 1)
    )
    newer_done = await _make_practice(
        db_tx, client_id=client_id, type_id=type_id, status="completed", start=date(2023, 2, 1)
    )
    await _make_practice(
        db_tx,
        client_id=client_id,
        type_id=type_id,
        status="completed",
        start=date(2022, 2, 1),
        client_visible=False,
    )

    history = await _history(db_tx, client_id)

    assert [(h["id"], h["status"]) for h in history] == [
        (str(newer_done), "completed"),
        (str(old_expired), "expired"),
    ]
