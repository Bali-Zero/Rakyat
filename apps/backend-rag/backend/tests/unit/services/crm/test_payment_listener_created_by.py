"""Round-2 R5(b): payment-received mails fall back from assigned_to to created_by.

When ``assigned_to`` is empty, ``created_by`` addresses the team mail and is
CC'd on the client mail; when both are set, ``assigned_to`` still wins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.services.crm.practice_status_listener import PracticeStatusListener
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL

ASSIGNEE = "assignee@example.test"
CREATOR = "creator@example.test"
CLIENT = "john@example.test"


@pytest.fixture
def listener():
    with patch("backend.services.crm.practice_status_listener.ProcessAutomationService") as MockPAS:
        MockPAS.return_value = AsyncMock()
        return PracticeStatusListener(db_dsn="postgres://fake:5432/test", db_pool=AsyncMock())


def _practice(**overrides):
    data = {
        "client_id": 1,
        "practice_type_name": "KITAS",
        "quoted_price": 5_000_000,
        "assigned_to": None,
        "created_by": CREATOR,
    }
    data.update(overrides)
    return data


# ── _on_payment_received recipient selection ────────────────────────────────


class TestOnPaymentReceivedRecipientFallback:
    @pytest.mark.asyncio
    async def test_created_by_used_when_assigned_to_missing(self, listener):
        listener._send_payment_emails = AsyncMock()
        await listener._on_payment_received(42, {"assigned_to": None, "created_by": CREATOR})
        listener._send_payment_emails.assert_awaited_once_with(42, triggered_by=CREATOR)

    @pytest.mark.asyncio
    async def test_assigned_to_wins_when_both_set(self, listener):
        listener._send_payment_emails = AsyncMock()
        await listener._on_payment_received(42, {"assigned_to": ASSIGNEE, "created_by": CREATOR})
        listener._send_payment_emails.assert_awaited_once_with(42, triggered_by=ASSIGNEE)

    @pytest.mark.asyncio
    async def test_accounting_fallback_when_both_missing(self, listener):
        listener._send_payment_emails = AsyncMock()
        await listener._on_payment_received(42, {})
        listener._send_payment_emails.assert_awaited_once_with(42, triggered_by=ACCOUNTING_EMAIL)


# ── _send_payment_emails team mail + client CC ──────────────────────────────


class TestSendPaymentEmailsCreatedByFallback:
    @pytest.mark.asyncio
    async def test_created_by_addresses_team_mail_and_client_cc(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(return_value=_practice())
        listener._process_svc._fetch_client_data = AsyncMock(
            return_value={"full_name": "John Doe", "email": CLIENT}
        )
        listener._send_via_internal_api = AsyncMock()

        await listener._send_payment_emails(42, triggered_by=CREATOR)

        assert listener._send_via_internal_api.await_count == 2
        client_call = listener._send_via_internal_api.await_args_list[0]
        assert client_call.args[0] == CLIENT
        assert client_call.kwargs["cc"] == CREATOR
        team_call = listener._send_via_internal_api.await_args_list[1]
        assert team_call.args[0] == CREATOR
        assert team_call.kwargs["cc"] == ACCOUNTING_EMAIL

    @pytest.mark.asyncio
    async def test_assigned_to_wins_for_team_mail_and_client_cc(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(
            return_value=_practice(assigned_to=ASSIGNEE)
        )
        listener._process_svc._fetch_client_data = AsyncMock(
            return_value={"full_name": "John Doe", "email": CLIENT}
        )
        listener._send_via_internal_api = AsyncMock()

        await listener._send_payment_emails(42, triggered_by=ASSIGNEE)

        assert listener._send_via_internal_api.await_count == 2
        client_call = listener._send_via_internal_api.await_args_list[0]
        assert client_call.kwargs["cc"] == ASSIGNEE
        team_call = listener._send_via_internal_api.await_args_list[1]
        assert team_call.args[0] == ASSIGNEE

    @pytest.mark.asyncio
    async def test_no_team_mail_without_assignee_or_creator(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(
            return_value=_practice(assigned_to=None, created_by=None)
        )
        listener._process_svc._fetch_client_data = AsyncMock(
            return_value={"full_name": "John Doe", "email": CLIENT}
        )
        listener._send_via_internal_api = AsyncMock()

        await listener._send_payment_emails(42, triggered_by=ACCOUNTING_EMAIL)

        assert listener._send_via_internal_api.await_count == 1
        client_call = listener._send_via_internal_api.await_args_list[0]
        assert client_call.args[0] == CLIENT
        assert client_call.kwargs["cc"] is None
