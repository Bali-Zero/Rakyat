"""C1: a completed practice's date, owner and service anchor the delivery bonus; only an admin may change them."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

COMPLETED_AT = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
CREATOR = {"email": "creator@example.invalid", "role": "consultant"}


def _row(status):
    return {
        "id": 1,
        "client_id": 42,
        "practice_type_id": 10,
        "practice_type_code": "ext_b1_voa",
        "practice_type_name": "Extension",
        "status": status,
        "priority": "normal",
        "payment_status": "paid",
        "client_visible": False,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "created_by": CREATOR["email"],
        "assigned_to": CREATOR["email"],
        "completion_date": COMPLETED_AT,
        "client_lead": None,
        "start_date": None,
        "discount_amount": None,
        "metadata": {},
        "quoted_price": None,
    }


async def _patch(mock_db_pool, mock_db_conn, *, status="completed", admin=False, **fields):
    from backend.app.routers import crm_practices as router

    txn = MagicMock()
    txn.__aenter__ = AsyncMock()
    txn.__aexit__ = AsyncMock(return_value=False)
    mock_db_conn.transaction = MagicMock(return_value=txn)
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_row(status)] * 3)
    mock_db_conn.execute = AsyncMock(return_value="OK")
    user = {"email": "admin@example.invalid", "role": "admin"} if admin else CREATOR
    with (
        patch.object(router, "process_assignees", AsyncMock(return_value=None)),
        patch.object(router, "invalidate_cache", new=AsyncMock()),
        patch.object(router, "spawn"),
    ):
        try:
            await router.update_practice(
                request=MagicMock(),
                practice_id=1,
                updates=router.PracticeUpdate(**fields),
                db_pool=mock_db_pool,
                current_user=user,
            )
        except HTTPException as exc:
            return exc
    return None


def _wrote(conn):
    calls = [str(c.args[0]) for c in conn.execute.await_args_list + conn.fetchrow.await_args_list]
    return [sql for sql in calls if "UPDATE practices" in sql or "INSERT INTO activity_log" in sql]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"completion_date": datetime(2026, 11, 5, 4, tzinfo=timezone.utc)},
        {"assigned_to": "someone.else@example.invalid"},
        # readers match the owner on LOWER(BTRIM()): a tab is another identity
        {"assigned_to": CREATOR["email"] + "\t"},
        # a later proof upload prices the bonus by the current service
        {"practice_type_code": "ext_c1_tourism"},
    ],
)
async def test_a_non_admin_cannot_move_the_date_or_owner_of_a_completed_practice(
    mock_db_pool, mock_db_conn, fields
):
    caught = await _patch(mock_db_pool, mock_db_conn, **fields)
    assert caught is not None and caught.status_code == 409
    assert "admin" in caught.detail
    assert _wrote(mock_db_conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"completion_date": datetime(2026, 11, 5, 4, tzinfo=timezone.utc)},
        {"assigned_to": "someone.else@example.invalid"},
    ],
)
async def test_an_admin_can_move_them(mock_db_pool, mock_db_conn, fields):
    caught = await _patch(mock_db_pool, mock_db_conn, admin=True, **fields)
    assert caught is None
    assert any("UPDATE practices" in sql for sql in _wrote(mock_db_conn))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"completion_date": COMPLETED_AT},
        {"assigned_to": CREATOR["email"].upper()},
        {"assigned_to": "  " + CREATOR["email"] + " "},
        {"notes": "a note"},
    ],
)
async def test_the_same_value_or_another_field_is_not_a_change(mock_db_pool, mock_db_conn, fields):
    caught = await _patch(mock_db_pool, mock_db_conn, **fields)
    assert caught is None
    assert any("UPDATE practices" in sql for sql in _wrote(mock_db_conn))


@pytest.mark.asyncio
async def test_an_open_practice_is_unchanged(mock_db_pool, mock_db_conn):
    caught = await _patch(
        mock_db_pool,
        mock_db_conn,
        status="on_process",
        completion_date=datetime(2026, 11, 5, 4, tzinfo=timezone.utc),
        assigned_to="someone.else@example.invalid",
    )
    assert caught is None
    assert any("UPDATE practices" in sql for sql in _wrote(mock_db_conn))
