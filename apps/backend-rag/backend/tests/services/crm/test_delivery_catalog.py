from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.app.routers.crm_practices import (
    PracticeCreate,
    PracticeUpdate,
    create_practice,
    update_practice,
)
from backend.services.crm.delivery_catalog import _catalog, delivery_policy


def test_issuance_is_its_own_event_and_post_entry_slots_are_required():
    for code, policy in _catalog().items():
        keys = [slot["key"] for slot in policy["slots"]]
        assert policy["slots"] and len(keys) == len(set(keys)), code
        if "electronic_visa" in keys:
            assert policy["bonus_slot"] == "electronic_visa", code
            if code.startswith("visa_"):
                assert any(
                    "ITK" in s["label"] and s["condition"] is None for s in policy["slots"]
                ), code
        else:
            assert policy["bonus_slot"] is None, code
    assert delivery_policy("visa_c1_tourism")["bonus_slot"] == "electronic_visa"
    assert delivery_policy("kitas_investor_2yr_offshore")["bonus_slot"] == "electronic_visa"
    renewal = delivery_policy("kitas_working_extend")
    assert renewal["bonus_slot"] is None
    assert any(
        s["label"].startswith("Renewed IMK") and not s["condition"] for s in renewal["slots"]
    )
    assert delivery_policy("kitas_working_onshore")["slots"][0]["condition"]
    assert delivery_policy("visa_voa") == delivery_policy("visa_b1_voa")
    assert delivery_policy("other_driving_license")
    assert delivery_policy("open_inquiry") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["on_process", "completed"])
async def test_ordinary_create_cannot_bypass_accounting_or_delivery(status):
    pool = MagicMock()
    with pytest.raises(HTTPException) as error:
        await create_practice(
            MagicMock(),
            PracticeCreate(client_id=1, practice_type_code="visa_c1_tourism", status=status),
            MagicMock(),
            pool,
            {"email": "employee@example.test"},
        )
    assert error.value.status_code == 409
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"status": "on_process"},
        {"status": "completed"},
        {"payment_status": "paid"},
        {"paid_amount": 200},
    ],
)
async def test_staff_cannot_patch_workflow_owned_fields(values):
    pool, conn = MagicMock(), AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.fetchrow.return_value = {
        "status": "sending_invoice",
        "client_id": 1,
        "client_visible": True,
        "created_by": "employee@example.test",
    }
    with pytest.raises(HTTPException) as error:
        await update_practice(
            MagicMock(),
            8,
            PracticeUpdate(**values),
            pool,
            {"role": "admin", "email": "employee@example.test"},
        )
    assert error.value.status_code == 409
    conn.execute.assert_not_called()
