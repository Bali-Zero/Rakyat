"""Tests for admin CRM KG Drive backfill trigger."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

_UNSET = object()
_DEFAULT_ADMIN = {"role": "admin", "user_id": "admin-1"}


def _request_with_pool(pool: object, user: object = _UNSET) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        state=SimpleNamespace(user=dict(_DEFAULT_ADMIN) if user is _UNSET else user),
    )


@pytest.mark.asyncio
async def test_backfill_drive_documents_dry_run_returns_summary() -> None:
    from backend.app.routers.admin_crm_kg import trigger_backfill_drive_documents

    with patch(
        "backend.services.documents.crm_drive_backfill_service.run_crm_drive_backfill",
        new_callable=AsyncMock,
        return_value={"dry_run": True, "candidate_count": 3},
    ) as mock_backfill:
        result = await trigger_backfill_drive_documents(
            request=_request_with_pool(object()),
            background_tasks=BackgroundTasks(),
            limit=10,
            dry_run=True,
            client_id=42,
            allow_ocr=False,
        )

    assert result == {"dry_run": True, "candidate_count": 3}
    mock_backfill.assert_called_once()
    assert mock_backfill.call_args.kwargs["limit"] == 10
    assert mock_backfill.call_args.kwargs["dry_run"] is True
    assert mock_backfill.call_args.kwargs["client_id"] == 42
    assert mock_backfill.call_args.kwargs["allow_ocr"] is False


@pytest.mark.asyncio
async def test_backfill_drive_documents_live_runs_in_background() -> None:
    from backend.app.routers.admin_crm_kg import trigger_backfill_drive_documents

    background_tasks = BackgroundTasks()
    with patch(
        "backend.services.documents.crm_drive_backfill_service.run_crm_drive_backfill",
        new_callable=AsyncMock,
    ) as mock_backfill:
        result = await trigger_backfill_drive_documents(
            request=_request_with_pool(object()),
            background_tasks=background_tasks,
            limit=5,
            dry_run=False,
            client_id=None,
            allow_ocr=False,
        )

    assert result["status"] == "started"
    assert result["dry_run"] is False
    assert result["allow_ocr"] is False
    assert len(background_tasks.tasks) == 1
    mock_backfill.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_drive_documents_live_requires_explicit_ocr_flag() -> None:
    from backend.app.routers.admin_crm_kg import trigger_backfill_drive_documents

    background_tasks = BackgroundTasks()
    with patch(
        "backend.services.documents.crm_drive_backfill_service.run_crm_drive_backfill",
        new_callable=AsyncMock,
    ) as mock_backfill:
        result = await trigger_backfill_drive_documents(
            request=_request_with_pool(object()),
            background_tasks=background_tasks,
            limit=5,
            dry_run=False,
            client_id=None,
            allow_ocr=True,
        )

    assert result["status"] == "started"
    assert result["allow_ocr"] is True

    task = background_tasks.tasks[0]
    await task.func(*task.args, **task.kwargs)
    assert mock_backfill.call_args.kwargs["allow_ocr"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "expected_status"),
    [
        (None, 401),
        ({"role": "team", "user_id": "staff-1"}, 403),
    ],
)
async def test_crm_kg_mutating_routes_require_admin(
    user: object,
    expected_status: int,
) -> None:
    from backend.app.routers.admin_crm_kg import (
        trigger_backfill_drive_documents,
        trigger_build_mediated,
        trigger_garbage_collect,
    )

    req = _request_with_pool(object(), user=user)
    with pytest.raises(HTTPException) as exc1:
        await trigger_backfill_drive_documents(
            request=req,
            background_tasks=BackgroundTasks(),
            limit=5,
            dry_run=True,
            client_id=None,
            allow_ocr=False,
        )
    assert exc1.value.status_code == expected_status

    with pytest.raises(HTTPException) as exc2:
        await trigger_build_mediated(request=req, background_tasks=BackgroundTasks())
    assert exc2.value.status_code == expected_status

    with pytest.raises(HTTPException) as exc3:
        await trigger_garbage_collect(request=req, background_tasks=BackgroundTasks())
    assert exc3.value.status_code == expected_status
