"""The listener and a confirmation that starts the case (owner rule a).

A live confirmation paid in full moves a case from sending_invoice to on_process in the same
statement as the payment. The listener receives ONE change carrying both: it is not historical
(its status moved), it sends the payment emails once, then reaches the on_process milestone,
whose start emails the automation skips for a paid case.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.crm.practice_status_listener import PracticeStatusListener


@pytest.fixture
def listener():
    with patch("backend.services.crm.practice_status_listener.ProcessAutomationService") as pas:
        pas.return_value = AsyncMock()
        return PracticeStatusListener(db_dsn="postgres://fake:5432/test", db_pool=AsyncMock())


@pytest.mark.asyncio
async def test_one_change_with_payment_and_start_runs_both_handlers_once_in_order(listener):
    calls: list[str] = []
    listener._on_payment_received = AsyncMock(side_effect=lambda *a, **k: calls.append("payment"))
    listener._on_status_changed = AsyncMock(side_effect=lambda *a, **k: calls.append("status"))
    historical = AsyncMock(return_value=True)
    payload = json.dumps(
        {
            "practice_id": 7,
            "old_status": "sending_invoice",
            "new_status": "on_process",
            "old_payment": "unpaid",
            "new_payment": "paid",
            "ts": "1782345600.123456",
        }
    )
    with patch(
        "backend.services.crm.practice_status_listener.recorded_without_notifications", historical
    ):
        await listener._handle_notification(payload)

    historical.assert_not_awaited()  # a moved status is never a historical change
    assert calls == ["payment", "status"]
    listener._on_status_changed.assert_awaited_once()
    assert listener._on_status_changed.await_args.args[:2] == (7, "on_process")


@pytest.mark.asyncio
async def test_the_start_milestone_hands_a_paid_case_to_the_automation(listener):
    """The automation decides (trigger_on_process_start skips the start emails when paid)."""
    await listener._on_status_changed(7, "on_process", {"assigned_to": "staff@example.test"})
    listener._process_svc.trigger_on_process_start.assert_awaited_once_with(
        practice_id=7, triggered_by="staff@example.test"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_status", "new_status", "old_payment", "new_payment"),
    [
        ("on_process", "sending_invoice", "paid", "unpaid"),  # undo of a confirm that started it
        ("on_process", "on_process", "paid", "partial"),  # undo of the last part
        ("on_process", "on_process", "partial", "unpaid"),  # undo of a first part
    ],
)
async def test_an_undone_confirmation_sends_nothing(
    listener, old_status, new_status, old_payment, new_payment
):
    """Owner rule (b): the change an undo makes is neither a payment becoming paid nor a status
    entering a client milestone, so no handler runs."""
    listener._on_payment_received = AsyncMock()
    listener._on_status_changed = AsyncMock()
    historical = AsyncMock(return_value=False)
    payload = json.dumps(
        {
            "practice_id": 7,
            "old_status": old_status,
            "new_status": new_status,
            "old_payment": old_payment,
            "new_payment": new_payment,
            "ts": "1782345600.123456",
        }
    )
    with patch(
        "backend.services.crm.practice_status_listener.recorded_without_notifications", historical
    ):
        await listener._handle_notification(payload)
    listener._on_payment_received.assert_not_awaited()
    listener._on_status_changed.assert_not_awaited()
