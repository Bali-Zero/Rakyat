"""K127 — PPh withholding on a process: the CRM API contract.

``POST /api/crm/practices`` and ``PATCH /api/crm/practices/{id}`` accept an optional
``pph = {"type", "rate_percent"}`` (or ``null`` on PATCH to clear it). Only the tax team
and CRM admins may send it; every value lands in ``practices.metadata.pph`` with an
audit entry in ``metadata.pph_log``. Driven through a real FastAPI app so the status
codes are the ones a caller sees. Database fully mocked; all people are synthetic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import crm_practices

TAX_LEAD = {"id": "u-tax", "email": "tax.lead@example.test", "role": "tax lead"}
CONSULTANT = {"id": "u-con", "email": "consultant@example.test", "role": "consultant"}


def _client(pool: MagicMock, user: dict[str, Any]) -> TestClient:
    app = FastAPI()
    app.include_router(crm_practices.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: pool
    return TestClient(app)


def _old_row(owner: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "status": "on_process",
        "client_id": 42,
        "client_visible": True,
        "created_by": owner,
        "assigned_to": owner,
        "start_date": None,
        "completion_date": None,
        "discount_amount": None,
        "metadata": {},
        "quoted_price": Decimal("2500000"),
        "client_lead": owner,
        "practice_type_code": "tax_monthly",
    }
    return {**row, **overrides}


def _updated_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 7,
        "status": "on_process",
        "client_id": 42,
        "client_visible": True,
        "practice_type_id": 10,
        "practice_type_code": "tax_monthly",
        "practice_type_name": "Synthetic Monthly Tax",
        "invoice_company_id": None,
    }
    return {**row, **overrides}


def _patch(pool: MagicMock, user: dict[str, Any], body: dict[str, Any]):
    with (
        patch.object(crm_practices, "invalidate_cache", new=AsyncMock()),
        patch.object(crm_practices, "spawn", new=MagicMock()),
    ):
        return _client(pool, user).patch("/api/crm/practices/7", json=body)


def _update_call(conn: MagicMock):
    """(query, params) of the UPDATE ... RETURNING statement."""
    for call in conn.fetchrow.await_args_list:
        if "UPDATE practices" in call.args[0]:
            return call.args[0], call.args[1:]
    raise AssertionError("practice UPDATE never ran")


def _metadata_param(conn: MagicMock) -> dict[str, Any]:
    query, params = _update_call(conn)
    for i, value in enumerate(params, start=1):
        if f"metadata = ${i}::jsonb" in query:
            return value
    raise AssertionError("metadata was not written by the UPDATE")


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_tax_lead_sets_pph23_2_percent(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_old_row(TAX_LEAD["email"]), _updated_row()])
    response = _patch(mock_db_pool, TAX_LEAD, {"pph": {"type": "PPH23", "rate_percent": 2}})

    assert response.status_code == 200, response.text
    meta = _metadata_param(mock_db_conn)
    assert meta["pph"] == {"type": "PPH23", "rate_percent": 2.0}
    (entry,) = meta["pph_log"]
    assert entry["previous"] is None
    assert entry["value"] == {"type": "PPH23", "rate_percent": 2.0}
    assert entry["changed_by"] == TAX_LEAD["email"]
    assert entry["event"] == "update"
    assert entry["changed_at"]


def test_change_keeps_other_metadata_and_logs_old_to_new(mock_db_pool, mock_db_conn):
    old_meta = {
        "discount_log": [{"amount": "100000", "event": "create"}],
        "pph": {"type": "PPH23", "rate_percent": 2.0},
        "pph_log": [{"previous": None, "value": {"type": "PPH23", "rate_percent": 2.0}}],
    }
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[_old_row(TAX_LEAD["email"], metadata=old_meta), _updated_row()]
    )
    response = _patch(mock_db_pool, TAX_LEAD, {"pph": {"type": "PPH21", "rate_percent": "2.5"}})

    assert response.status_code == 200, response.text
    meta = _metadata_param(mock_db_conn)
    assert meta["discount_log"] == old_meta["discount_log"]
    assert meta["pph"] == {"type": "PPH21", "rate_percent": 2.5}
    assert len(meta["pph_log"]) == 2
    assert meta["pph_log"][-1]["previous"] == {"type": "PPH23", "rate_percent": 2.0}
    assert meta["pph_log"][-1]["value"] == {"type": "PPH21", "rate_percent": 2.5}


def test_null_clears_pph_and_logs_it(mock_db_pool, mock_db_conn):
    old_meta = {"pph": {"type": "PPH23", "rate_percent": 2.0}, "discount_log": []}
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[_old_row(TAX_LEAD["email"], metadata=old_meta), _updated_row()]
    )
    response = _patch(mock_db_pool, TAX_LEAD, {"pph": None})

    assert response.status_code == 200, response.text
    meta = _metadata_param(mock_db_conn)
    assert "pph" not in meta
    assert meta["discount_log"] == []
    assert meta["pph_log"][-1]["previous"] == {"type": "PPH23", "rate_percent": 2.0}
    assert meta["pph_log"][-1]["value"] is None
    assert meta["pph_log"][-1]["event"] == "clear"


@pytest.mark.parametrize("body", [{"pph": {"type": "PPH23", "rate_percent": 2}}, {"pph": None}])
def test_consultant_gets_403_and_practice_is_untouched(mock_db_pool, mock_db_conn, body):
    # The consultant owns the practice, so only the PPh rule can refuse them.
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_old_row(CONSULTANT["email"]), _updated_row()])
    response = _patch(mock_db_pool, CONSULTANT, {**body, "notes": "synthetic note"})

    assert response.status_code == 403
    assert mock_db_conn.fetchrow.await_count == 1, "only the read of the old row"
    mock_db_conn.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "pph",
    [
        {"type": "PPH23", "rate_percent": 0},
        {"type": "PPH23", "rate_percent": 10.5},
        {"type": "PPH23", "rate_percent": "2.125"},
        {"type": "PPH23", "rate_percent": -2},
        {"type": "PPN", "rate_percent": 2},
        {"type": "pph23", "rate_percent": 2},
        {"rate_percent": 2},
    ],
)
def test_invalid_pph_is_422_before_any_db_access(mock_db_pool, mock_db_conn, pph):
    response = _patch(mock_db_pool, TAX_LEAD, {"pph": pph})

    assert response.status_code == 422
    mock_db_conn.fetchrow.assert_not_awaited()


def test_boundary_rates_are_accepted(mock_db_pool, mock_db_conn):
    for rate, stored in (("10", 10.0), ("0.01", 0.01)):
        mock_db_conn.fetchrow = AsyncMock(side_effect=[_old_row(TAX_LEAD["email"]), _updated_row()])
        response = _patch(mock_db_pool, TAX_LEAD, {"pph": {"type": "PPH4_2", "rate_percent": rate}})
        assert response.status_code == 200, response.text
        assert _metadata_param(mock_db_conn)["pph"] == {"type": "PPH4_2", "rate_percent": stored}


def test_patch_without_pph_does_not_touch_metadata(mock_db_pool, mock_db_conn):
    """Innocence: a PATCH that does not mention pph writes exactly what it wrote before."""
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_old_row(CONSULTANT["email"]), _updated_row()])
    response = _patch(mock_db_pool, CONSULTANT, {"notes": "synthetic note"})

    assert response.status_code == 200, response.text
    query, params = _update_call(mock_db_conn)
    assert "metadata" not in query
    assert params[0] == "synthetic note"


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


def _type_row() -> dict[str, Any]:
    return {"id": 10, "base_price": Decimal("2500000"), "category": "tax", "name": "Synthetic Tax"}


def _inserted_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 7,
        "uuid": uuid.uuid4(),
        "client_id": 42,
        "practice_type_id": 10,
        "status": "inquiry",
        "priority": "normal",
        "quoted_price": Decimal("3000000"),
        "discount_amount": Decimal("500000"),
        "discount_reason": None,
        "actual_price": None,
        "payment_status": "unpaid",
        "assigned_to": "assignee@example.test",
        "start_date": None,
        "completion_date": None,
        "expiry_date": None,
        "created_at": datetime(2026, 11, 19, tzinfo=timezone.utc),
        "family_member_id": None,
        "invoice_company_id": None,
    }
    return {**row, **overrides}


def _post(pool: MagicMock, user: dict[str, Any], body: dict[str, Any]):
    with (
        patch.object(crm_practices, "invalidate_cache", new=AsyncMock()),
        patch(
            "backend.services.crm.welcome.welcome_practice_service.send_practice_kickoff",
            new=AsyncMock(),
        ),
    ):
        return _client(pool, user).post("/api/crm/practices/", json=body)


def _insert_metadata(conn: MagicMock) -> dict[str, Any]:
    for call in conn.fetchrow.await_args_list:
        query = call.args[0]
        if "INSERT INTO practices" in query:
            columns = query.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            index = columns.index("metadata")
            assert f"${index + 1}::jsonb" in query
            return call.args[1 + index]
    raise AssertionError("practice INSERT never ran")


def test_tax_lead_creates_process_with_discount_and_pph(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_type_row(), _inserted_row()])
    body = {
        "client_id": 42,
        "practice_type_code": "tax_monthly",
        "quoted_price": 3000000,
        "discount_amount": 500000,
        "assigned_to": TAX_LEAD["email"],
        "pph": {"type": "PPH23", "rate_percent": 2},
    }
    response = _post(mock_db_pool, TAX_LEAD, body)

    assert response.status_code == 200, response.text
    meta = _insert_metadata(mock_db_conn)
    assert meta["pph"] == {"type": "PPH23", "rate_percent": 2.0}
    assert meta["pph_log"][0]["event"] == "create"
    assert meta["pph_log"][0]["changed_by"] == TAX_LEAD["email"]
    assert meta["discount_log"][0]["amount"] == "500000"


def test_consultant_create_with_pph_is_403_before_any_write(mock_db_pool, mock_db_conn):
    body = {
        "client_id": 42,
        "practice_type_code": "tax_monthly",
        "assigned_to": CONSULTANT["email"],
        "pph": {"type": "PPH23", "rate_percent": 2},
    }
    response = _post(mock_db_pool, CONSULTANT, body)

    assert response.status_code == 403
    mock_db_conn.fetchrow.assert_not_awaited()
    mock_db_conn.execute.assert_not_awaited()


def test_create_without_pph_writes_no_metadata(mock_db_pool, mock_db_conn):
    """Innocence: no discount and no pph -> no metadata column in the INSERT, as before."""
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[_type_row(), _inserted_row(discount_amount=None)]
    )
    body = {
        "client_id": 42,
        "practice_type_code": "tax_monthly",
        "assigned_to": CONSULTANT["email"],
    }
    response = _post(mock_db_pool, CONSULTANT, body)

    assert response.status_code == 200, response.text
    insert = next(
        c.args[0]
        for c in mock_db_conn.fetchrow.await_args_list
        if "INSERT INTO practices" in c.args[0]
    )
    assert "metadata" not in insert
