"""Regression coverage for failed required writes and failed transaction commits."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("failure", ["required_write", "commit"])
async def test_failed_operation_exits_transaction_without_notifications(
    mock_db_pool, mock_db_conn, operation, failure
):
    from backend.app.routers import crm_practices as router

    outer = MagicMock()
    outer.__aenter__ = AsyncMock()

    async def finish(exc_type, exc, traceback):
        if failure == "commit" and exc is None:
            raise asyncpg.CheckViolationError("synthetic deferred failure")
        return False

    outer.__aexit__ = AsyncMock(side_effect=finish)
    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock()
    savepoint.__aexit__ = AsyncMock(return_value=False)
    mock_db_conn.transaction = MagicMock(side_effect=[outer, savepoint, savepoint])

    row = {
        "id": 1,
        "client_id": 42,
        "practice_type_id": 10,
        "practice_type_code": "open_inquiry" if operation == "create" else "ext_b1_voa",
        "practice_type_name": "Open inquiry",
        "status": "inquiry" if operation == "create" else "on_process",
        "priority": "normal",
        "payment_status": "unpaid",
        "client_visible": False,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "created_by": "actor@example.invalid",
    }
    previous = {**row, "status": "inquiry"}
    lookup = {"id": 10, "name": "Open inquiry", "category": "practice", "base_price": 0}
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[lookup if operation == "create" else previous, row]
    )

    async def execute(sql, *args):
        required = "UPDATE clients" if operation == "create" else "INSERT INTO activity_log"
        if failure == "required_write" and required in sql:
            raise asyncpg.CheckViolationError("synthetic required write failure")
        return "OK"

    mock_db_conn.execute = AsyncMock(side_effect=execute)
    user = {"email": "actor@example.invalid", "role": "admin"}
    background = MagicMock()
    service = MagicMock()
    service.notify_practice_status_changed.return_value = None
    with (
        patch.object(router, "is_crm_admin", return_value=True),
        patch.object(router, "validate_transition"),
        patch.object(router, "invalidate_cache", new=AsyncMock()),
        patch.object(router, "spawn") as spawn,
        patch(
            "backend.services.portal.portal_notification_service.PortalNotificationService",
            return_value=service,
        ),
    ):
        with pytest.raises(HTTPException) as caught:
            if operation == "create":
                await router.create_practice(
                    request=MagicMock(),
                    practice=router.PracticeCreate(
                        client_id=42, assigned_to="actor@example.invalid"
                    ),
                    background_tasks=background,
                    db_pool=mock_db_pool,
                    current_user=user,
                )
            else:
                await router.update_practice(
                    request=MagicMock(),
                    practice_id=1,
                    updates=router.PracticeUpdate(status="on_process"),
                    db_pool=mock_db_pool,
                    current_user=user,
                )
        assert caught.value.status_code == 400
        outer.__aenter__.assert_awaited_once()
        outer.__aexit__.assert_awaited_once()
        exception_at_exit = outer.__aexit__.await_args.args[1]
        if failure == "required_write":
            assert isinstance(exception_at_exit, asyncpg.CheckViolationError)
        else:
            assert exception_at_exit is None
        spawn.assert_not_called()
        service.notify_practice_status_changed.assert_not_called()
        background.add_task.assert_not_called()
