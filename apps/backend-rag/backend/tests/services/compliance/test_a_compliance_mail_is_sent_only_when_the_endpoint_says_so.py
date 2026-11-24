"""A compliance mail counts as sent only when the send endpoint says it was sent.

The internal send endpoint answers HTTP 200 with ``success: false`` when every provider
failed, and with ``delivery_uncertain`` when a keyed send cannot be confirmed. The LKPM deadline
notifier, the visa expiry notifier and the alert dispatcher's mail adapter looked at the status
code only: a reminder nobody received was counted as sent, and the dispatcher wrote a delivery
row for it in the audit log.

What these tests do not claim: a retry. The alerts engine does not dispatch again an alert it
already holds at the same severity, so a client mail that failed is not sent on the next scan.
The dispatcher is called here directly, once per case.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import httpx
import pytest

import backend.services.compliance.alert_wiring as wiring
import backend.services.compliance.lkpm_deadline_notifier as lkpm_mod
import backend.services.compliance.visa_expiry_team_notifier as visa_mod
from backend.services.compliance.alert_dispatcher import AlertDispatcher
from backend.services.compliance.alert_repository import AlertRow
from backend.services.notifications.email_audit import EmailNotDelivered

ALL_PROVIDERS_FAILED = {"success": False, "message": "All providers failed: brevo 401"}
UNCERTAIN = {"success": False, "message": "delivery uncertain", "delivery_uncertain": True}
SENT = {"success": True, "message": "Email sent"}

NOT_SENT = [
    pytest.param(ALL_PROVIDERS_FAILED, id="every provider failed"),
    pytest.param(UNCERTAIN, id="delivery uncertain"),
]


def _http_client(status: int, body: Any, calls: list[httpx.Request] | None = None):
    """A real httpx client whose transport answers every request the same way."""

    def answer(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


# ── the LKPM deadline notifier ────────────────────────────────────────────────


def _lkpm_notifier(monkeypatch: pytest.MonkeyPatch, status: int, body: Any):
    connection = AsyncMock()
    connection.fetchval = AsyncMock(return_value="true")  # the kill switch is open
    acquired = AsyncMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquired)

    row = {
        "id": 1,
        "client_id": 100,
        "quarter": "Q1",
        "year": 2026,
        "status": "draft",
        "oss_submitted": False,
        "lkpm_assigned_to": None,
        "company_name": "PT Zz Example",
        "oss_username": "zz_oss",
    }
    monkeypatch.setattr(
        lkpm_mod.LKPMDeadlineNotifier, "_get_pending_reports", AsyncMock(return_value=[row])
    )
    monkeypatch.setattr(lkpm_mod, "_compute_days_until_deadline", lambda quarter, year, now: 5)
    monkeypatch.setattr(
        lkpm_mod.LKPMDeadlineNotifier, "_maybe_send_telegram", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        lkpm_mod.LKPMDeadlineNotifier,
        "_get_http_client",
        staticmethod(lambda: _http_client(status, body)),
    )
    return lkpm_mod.LKPMDeadlineNotifier(pool)


@pytest.mark.parametrize("body", NOT_SENT)
async def test_the_lkpm_reminder_is_not_counted_when_the_endpoint_says_not_sent(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    result = await _lkpm_notifier(monkeypatch, 200, body).check_and_notify()

    assert result["emails_sent"] == 0
    assert result["emails_failed"] == 1


async def test_the_lkpm_reminder_is_counted_when_the_endpoint_says_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _lkpm_notifier(monkeypatch, 200, SENT).check_and_notify()

    assert result["emails_sent"] == 1
    assert result["emails_failed"] == 0


async def test_the_lkpm_reminder_that_fails_at_the_endpoint_is_counted_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _lkpm_notifier(monkeypatch, 500, {"detail": "boom"}).check_and_notify()

    assert result["emails_sent"] == 0
    assert result["emails_failed"] == 1


# ── the visa expiry notifier ──────────────────────────────────────────────────


def _visa_notifier(monkeypatch: pytest.MonkeyPatch, status: int, body: Any):
    row = {
        "client_id": 1,
        "client_name": "Zz Client",
        "client_email": "client@example.test",
        "client_phone": "+62000",
        "nationality": "US",
        "assigned_to": "leader@example.test",
        "document_type": "visa",
        "expiry_date": date(2027, 5, 1),
        "days_until_expiry": 21,
        "current_visa_type": "B211",
        "current_visa_sponsor": "PT Zz",
        "passport_expiry": date(2028, 1, 1),
    }
    monkeypatch.setattr(
        visa_mod.VisaExpiryTeamNotifier, "_get_expiring_documents", AsyncMock(return_value=[row])
    )
    monkeypatch.setattr(
        visa_mod.VisaExpiryTeamNotifier,
        "_get_http_client",
        staticmethod(lambda: _http_client(status, body)),
    )
    return visa_mod.VisaExpiryTeamNotifier(MagicMock())


@pytest.mark.parametrize("body", NOT_SENT)
async def test_the_expiry_mails_are_failed_when_the_endpoint_says_not_sent(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """One mail to the team leader and the summary: neither was sent."""
    result = await _visa_notifier(monkeypatch, 200, body).check_and_notify()

    assert result == {"total_alerts": 1, "emails_sent": 0, "emails_failed": 2}


async def test_the_expiry_mails_are_sent_when_the_endpoint_says_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _visa_notifier(monkeypatch, 200, SENT).check_and_notify()

    assert result == {"total_alerts": 1, "emails_sent": 2, "emails_failed": 0}


# ── the dispatcher's mail adapter ─────────────────────────────────────────────


def _adapter(monkeypatch: pytest.MonkeyPatch, status: int, body: Any, calls=None):
    client = _http_client(status, body, calls)
    monkeypatch.setattr(wiring, "get_email_client", AsyncMock(return_value=client))
    return wiring.HttpEmailServiceAdapter()


async def _send(adapter: wiring.HttpEmailServiceAdapter) -> dict[str, Any]:
    return await adapter.send(
        from_="zantara@balizero.com", to="client@example.test", subject="s", body="b"
    )


async def test_the_adapter_raises_when_every_provider_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EmailNotDelivered) as refused:
        await _send(_adapter(monkeypatch, 200, ALL_PROVIDERS_FAILED))

    assert "All providers failed" in str(refused.value)
    assert refused.value.delivery_uncertain is False


async def test_the_adapter_raises_when_the_delivery_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EmailNotDelivered) as refused:
        await _send(_adapter(monkeypatch, 200, UNCERTAIN))

    assert refused.value.delivery_uncertain is True


async def test_the_adapter_returns_the_answer_of_a_mail_that_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    assert await _send(_adapter(monkeypatch, 200, SENT, calls)) == SENT
    assert len(calls) == 1


async def test_the_adapter_still_raises_on_an_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _send(_adapter(monkeypatch, 503, {"detail": "down"}))


# ── the dispatcher, with the real adapter, on the real delivery log ───────────

PORTAL_USER = "00000000-0000-0000-0000-0000000000a1"


def _alert(client_id: int) -> AlertRow:
    return AlertRow(
        alert_id="alert_mail_answer",
        client_id=client_id,
        category="visa_expiry",
        severity="urgent",
        status="pending",
        deadline=date(2027, 5, 1),
        days_until=7,
        compliance_item_ref="doc_1",
        dedup_key="visa:mail_answer:doc_1",
        message_it="messaggio",
        message_en="message",
        message_id="pesan",
        suggested_action="renew",
        estimated_cost_idr=None,
        evidence_refs=[],
        nb2_ref=None,
    )


async def _dispatch(db_tx: asyncpg.Connection, client_id: int, adapter) -> int:
    """Dispatch one alert whose client takes mail; the delivery rows of the mail channel."""
    await db_tx.execute(
        "INSERT INTO notification_prefs (user_id, email_enabled, wa_enabled) "
        "VALUES ($1::uuid, TRUE, FALSE) ON CONFLICT (user_id) DO UPDATE "
        "SET email_enabled = EXCLUDED.email_enabled, wa_enabled = EXCLUDED.wa_enabled",
        PORTAL_USER,
    )
    quiet = AsyncMock()
    dispatcher = AlertDispatcher.with_connection(
        db_tx,
        email_service=adapter,
        telegram_service=quiet,
        inapp_service=quiet,
        wa_service=quiet,
        _resolve_portal_user_id=lambda client_id: PORTAL_USER,
    )
    await dispatcher.dispatch(_alert(client_id))
    return await db_tx.fetchval(
        "SELECT count(*) FROM notification_log WHERE user_id = $1::uuid AND channel = 'email_client'",
        PORTAL_USER,
    )


@pytest.mark.integration
@pytest.mark.parametrize("body", NOT_SENT)
async def test_the_dispatcher_records_no_delivery_for_a_mail_that_was_not_sent(
    db_tx: asyncpg.Connection, sample_client, monkeypatch: pytest.MonkeyPatch, body
) -> None:
    calls: list[httpx.Request] = []
    adapter = _adapter(monkeypatch, 200, body, calls)

    assert await _dispatch(db_tx, sample_client["id"], adapter) == 0
    assert len(calls) == 1  # the send was tried, and no delivery was recorded for it


@pytest.mark.integration
async def test_the_dispatcher_records_the_delivery_of_a_mail_that_was_sent(
    db_tx: asyncpg.Connection, sample_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []
    adapter = _adapter(monkeypatch, 200, SENT, calls)

    assert await _dispatch(db_tx, sample_client["id"], adapter) == 1

    # the delivery row holds back a second mail for the same alert
    assert await _dispatch(db_tx, sample_client["id"], adapter) == 1
    assert len(calls) == 1
