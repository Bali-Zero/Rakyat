"""Assignment request validation for empty and explicit-null assignees."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.services.garuda_orders.idempotency import scoped_key_sha256
from backend.tests.services.garuda_portal.test_staff_router_transitions import (
    _ADMIN,
    _TEAM_A,
    _bearer,
    _create_and_pay_order,
    _make_app,
    _practice_id_for,
    _seed_team_member,
)

pytest_plugins = ("backend.tests.services.garuda_portal.test_staff_router_transitions",)


@pytest.mark.asyncio
async def test_empty_assignee_is_refused_without_writing_or_reserving_key(
    pool, order_repository
) -> None:
    await _seed_team_member(pool, _TEAM_A)
    order_id = await _create_and_pay_order(
        order_repository,
        result_id="result-empty-assignee0000",
        provider_event_id="evt-empty-assignee",
    )
    practice_id = await _practice_id_for(pool, order_id)
    await pool.execute(
        "UPDATE garuda_practices SET assigned_to = $2, assigned_at = statement_timestamp(), "
        "assigned_by = $3 WHERE practice_id = $1",
        practice_id,
        _TEAM_A,
        _ADMIN,
    )
    before = await pool.fetchrow(
        "SELECT assigned_to, assigned_at, assigned_by FROM garuda_practices WHERE practice_id = $1",
        practice_id,
    )
    key = "empty-assignee-key-00000001"
    app = _make_app(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/visa/voa/staff/practices/{practice_id}/assignment",
            headers={
                "Authorization": _bearer(_ADMIN, "admin"),
                "Idempotency-Key": key,
            },
            json={"assigned_to": ""},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    after = await pool.fetchrow(
        "SELECT assigned_to, assigned_at, assigned_by FROM garuda_practices WHERE practice_id = $1",
        practice_id,
    )
    assert dict(after) == dict(before)
    key_digest = scoped_key_sha256(actor=_ADMIN, operation="assignPractice", raw_key=key)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM garuda_order_idempotency WHERE key_sha256 = $1", key_digest
        )
        == 0
    )


@pytest.mark.asyncio
async def test_assignment_validation_keeps_null_valid_whitespace_and_omitted_behaviour(
    pool, order_repository
) -> None:
    await _seed_team_member(pool, _TEAM_A)
    order_id = await _create_and_pay_order(
        order_repository,
        result_id="result-assignee-validation0000",
        provider_event_id="evt-assignee-validation",
    )
    practice_id = await _practice_id_for(pool, order_id)
    app = _make_app(pool)
    headers = {"Authorization": _bearer(_ADMIN, "admin")}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assigned = await client.post(
            f"/api/visa/voa/staff/practices/{practice_id}/assignment",
            headers={**headers, "Idempotency-Key": "valid-assignee-key-00000001"},
            json={"assigned_to": _TEAM_A},
        )
        unassigned = await client.post(
            f"/api/visa/voa/staff/practices/{practice_id}/assignment",
            headers={**headers, "Idempotency-Key": "null-assignee-key-000000001"},
            json={"assigned_to": None},
        )
        whitespace = await client.post(
            f"/api/visa/voa/staff/practices/{practice_id}/assignment",
            headers={**headers, "Idempotency-Key": "space-assignee-key-00000001"},
            json={"assigned_to": "   "},
        )
        omitted = await client.post(
            f"/api/visa/voa/staff/practices/{practice_id}/assignment",
            headers={**headers, "Idempotency-Key": "omit-assignee-key-000000001"},
            json={},
        )
    assert assigned.status_code == 200
    assert assigned.json()["assigned_to"] == _TEAM_A
    assert unassigned.status_code == 200
    assert unassigned.json()["assigned_to"] is None
    assert whitespace.status_code == 422
    assert whitespace.json()["code"] == "INVALID_REQUEST"
    assert omitted.status_code == 422
    assert omitted.json()["code"] == "INVALID_REQUEST"
