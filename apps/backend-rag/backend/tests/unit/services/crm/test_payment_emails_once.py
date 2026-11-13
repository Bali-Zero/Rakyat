"""Payment-time emails go out once: unit-level rules (mocks, run in CI).

The real-Postgres twin is ``tests/db/test_payment_emails_once_pg.py`` (advisory lock, CHECK
constraints, ``practices.payment_status`` read). Placeholders only: no client data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.services.crm.practice_status_listener import PracticeStatusListener

TEAM = "team@example.test"
CLIENT = "client@example.test"
_MOD = "backend.services.crm.practice_status_listener"


@pytest.fixture
def listener():
    with patch(f"{_MOD}.ProcessAutomationService") as pas:
        pas.return_value = AsyncMock()
        inst = PracticeStatusListener(db_dsn="postgres://fake/test", db_pool=AsyncMock())
    inst._process_svc._fetch_practice_data = AsyncMock(
        return_value={"client_id": 1, "practice_type_name": "KITAS", "assigned_to": TEAM}
    )
    inst._send_via_internal_api = AsyncMock()
    return inst


def _client(email: str | None = CLIENT) -> AsyncMock:
    return AsyncMock(return_value={"full_name": "Placeholder Client", "email": email})


@pytest.mark.asyncio
async def test_first_paid_claims_and_sends_both_and_records_the_outcome(listener):
    """Spec B-1/B-4: the claim is won, both mails go out, each outcome is written."""
    listener._process_svc._fetch_client_data = _client()
    with (
        patch(f"{_MOD}.claim_email_once", AsyncMock(return_value=(True, 11))) as claim,
        patch(f"{_MOD}.log_email_attempt", AsyncMock(return_value=12)) as attempt,
        patch(f"{_MOD}.record_email_result", AsyncMock()) as result,
    ):
        await listener._send_payment_emails(42, triggered_by="x")

    assert listener._send_via_internal_api.await_count == 2
    assert claim.await_args.kwargs["email_type"] == "payment_client"
    assert claim.await_args.kwargs["once_group"] == ("payment_client", "payment_team")
    assert attempt.await_args.kwargs["email_type"] == "payment_team"
    assert [c.kwargs["status"] for c in result.await_args_list] == ["sent", "sent"]
    assert [c.args[1] for c in result.await_args_list] == [11, 12]


@pytest.mark.asyncio
async def test_a_lost_claim_sends_nothing(listener):
    """Spec B-2: payment emails already went out for this practice: no client, no team mail."""
    listener._process_svc._fetch_client_data = _client()
    with patch(f"{_MOD}.claim_email_once", AsyncMock(return_value=(False, None))):
        await listener._send_payment_emails(42, triggered_by="x")
    listener._send_via_internal_api.assert_not_called()


@pytest.mark.asyncio
async def test_without_client_email_the_team_mail_takes_the_claim(listener):
    """Spec B-4: no client address: the claim is for the team mail and reuses its row."""
    listener._process_svc._fetch_client_data = _client(None)
    with (
        patch(f"{_MOD}.claim_email_once", AsyncMock(return_value=(True, 21))) as claim,
        patch(f"{_MOD}.log_email_attempt", AsyncMock()) as attempt,
        patch(f"{_MOD}.record_email_result", AsyncMock()) as result,
    ):
        await listener._send_payment_emails(42, triggered_by="x")

    assert claim.await_args.kwargs["email_type"] == "payment_team"
    attempt.assert_not_called()
    assert listener._send_via_internal_api.await_count == 1
    assert result.await_args.args[1] == 21


@pytest.mark.asyncio
async def test_a_send_failure_is_recorded_and_pages_the_owner(listener):
    """Spec D3-1: a failed send leaves a 'failed' row and pages; the other mail still goes."""
    listener._process_svc._fetch_client_data = _client()
    listener._send_via_internal_api = AsyncMock(side_effect=[RuntimeError("down"), None])
    with (
        patch(f"{_MOD}.claim_email_once", AsyncMock(return_value=(True, 11))),
        patch(f"{_MOD}.log_email_attempt", AsyncMock(return_value=12)),
        patch(f"{_MOD}.record_email_result", AsyncMock()) as result,
        patch(f"{_MOD}.notify_email_failure_critical") as page,
    ):
        await listener._send_payment_emails(42, triggered_by="x")

    assert [c.kwargs["status"] for c in result.await_args_list] == ["failed", "sent"]
    page.assert_called_once()
    assert page.call_args.kwargs["email_type"] == "payment_client"
