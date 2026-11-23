from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routers.crm_visa_followup import OfficialNote
from backend.app.services.internal_email import EmailDeliveryUncertain, EmailProviderRejected
from backend.services.crm import visa_followup as tracking

DAY0 = date(2026, 1, 1)
USER = {"email": "consultant@example.test", "role": "consultant"}


def row(**kw):
    return {
        "practice_id": 7,
        "service_code": "visa_c1_tourism",
        "slot_key": "document_2",
        "started_on": DAY0,
        "due_on": DAY0 + timedelta(days=30),
        "allow_postpone": True,
        "fulfilled_at": None,
        "postponed_at": None,
        "postponement_note": None,
        "email_state": "not_sent",
        "escalation_note": None,
        "email_sent_at": None,
        "email_receipt_confirmed_at": None,
        "email_receipt_confirmed_by": None,
        "email_receipt_reference": None,
        "email_last_attempt_at": None,
        "email_requested_at": None,
        "email_requested_by": None,
        "email_attempts": 0,
        "email_idempotency_key": UUID("00000000-0000-4000-8000-000000000007"),
        **kw,
    }


@pytest.mark.parametrize(
    "age,status,postpone,escalate",
    [
        (0, "awaiting_permit", False, False),
        (29, "awaiting_permit", False, False),
        (30, "overdue", True, False),
        (45, "overdue", True, False),
        (59, "overdue", True, False),
        (60, "escalation_due", False, True),
        (61, "escalation_due", False, True),
    ],
)
def test_calendar_boundary(age, status, postpone, escalate):
    view = tracking.followup_view(row(), DAY0 + timedelta(days=age))
    assert (view["status"], view["can_postpone"], view["can_escalate"]) == (
        status,
        postpone,
        escalate,
    )


def test_no_entry_extension_never_resets_clock_and_cannot_be_repeated():
    active = row(
        postponed_at=datetime(2026, 2, 15, tzinfo=UTC),
        postponement_note="Travel postponed",
        due_on=DAY0 + timedelta(days=60),
    )
    view = tracking.followup_view(active, DAY0 + timedelta(days=59))
    assert (
        view["status"] == "awaiting_permit"
        and not view["can_postpone"]
        and view["days_remaining"] == 1
    )
    assert tracking.followup_view(active, DAY0 + timedelta(days=60))["can_escalate"]
    assert not tracking.followup_view(row(allow_postpone=False), DAY0 + timedelta(days=30))[
        "can_postpone"
    ]


def test_fulfilled_record_enables_neither_note_nor_send():
    view = tracking.followup_view(
        row(fulfilled_at=datetime(2026, 2, 2, tzinfo=UTC)), DAY0 + timedelta(days=70)
    )
    assert view["status"] == "fulfilled" and not view["can_escalate"] and not view["can_postpone"]


def test_only_nonempty_private_notes():
    assert OfficialNote(note="  Still awaiting arrival  ").note == "Still awaiting arrival"
    with pytest.raises(ValidationError):
        OfficialNote(note="  ")


@pytest.mark.asyncio
async def test_clock_starts_from_upload_not_official_issue_date_and_does_not_restart(monkeypatch):
    monkeypatch.setattr(
        tracking, "delivery_policy", lambda _: {"followup_slot": "document_2", "can_postpone": True}
    )
    monkeypatch.setattr(tracking, "business_today", lambda: DAY0)
    conn = AsyncMock()
    await tracking.record_visa_milestone(
        conn, {"id": 7, "code": "visa_c1_tourism"}, "electronic_visa"
    )
    first = conn.fetchval.await_args
    assert "ON CONFLICT(practice_id) DO NOTHING" in first.args[0]
    assert first.args[1:] == (
        7,
        "visa_c1_tourism",
        "document_2",
        True,
    )
    assert (
        "recorded_at AT TIME ZONE 'Asia/Makassar'" in first.args[0]
        and "day_zero+30" in first.args[0]
    )
    assert "earned_at" not in first.args[0]
    conn.reset_mock()
    await tracking.record_visa_milestone(conn, {"id": 7, "code": "visa_c1_tourism"}, "document_2")
    assert (
        len(conn.execute.await_args_list) == 1
        and "refresh_practice_visa_followup" in conn.execute.await_args.args[0]
    )
    monkeypatch.setattr(tracking, "delivery_policy", lambda _: {"followup_slot": None})
    conn.reset_mock()
    await tracking.record_visa_milestone(
        conn, {"id": 7, "code": "kitas_working_extend"}, "document_1"
    )
    conn.execute.assert_not_awaited()


@pytest.fixture
def db():
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool, conn


@pytest.mark.asyncio
async def test_private_note_is_saved_once_against_original_day60(db, monkeypatch):
    pool, conn = db
    received = row()
    monkeypatch.setattr(tracking, "_authorized_record", AsyncMock(return_value=received))
    monkeypatch.setattr(tracking, "business_today", lambda: DAY0 + timedelta(days=45))
    conn.fetchrow.return_value = row(
        postponed_at=datetime.now(UTC),
        postponement_note="No entry",
        due_on=DAY0 + timedelta(days=60),
    )
    assert (await tracking.postpone(pool, 7, USER, "No entry"))["days_remaining"] == 15
    assert "due_on=started_on+60" in conn.fetchrow.await_args.args[0]
    received.update(conn.fetchrow.return_value)
    conn.fetchrow.reset_mock()
    await tracking.postpone(pool, 7, USER, "No entry")
    conn.fetchrow.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        await tracking.postpone(pool, 7, USER, "A different reason")
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport,outcome",
    [
        (True, "sent"),
        (EmailProviderRejected("test"), "failed"),
        (EmailDeliveryUncertain("test"), "uncertain"),
    ],
)
async def test_authorized_mail_only_confirmed_acceptance_is_sent(
    db, monkeypatch, transport, outcome
):
    pool, conn = db
    monkeypatch.setattr(tracking, "business_today", lambda: DAY0 + timedelta(days=60))
    monkeypatch.setattr(tracking, "_authorized_record", AsyncMock(return_value=row()))
    stamp = datetime.now(UTC)
    claimed = row(
        escalation_note="Missing <permit>",
        email_state="sending",
        email_attempts=1,
        email_requested_by=USER["email"],
        email_last_attempt_at=stamp,
    )
    final = {
        **claimed,
        "email_state": outcome,
        "email_sent_at": stamp if outcome == "sent" else None,
    }
    conn.fetchrow.side_effect = [claimed, final]
    adapter = (
        AsyncMock(return_value=True) if transport is True else AsyncMock(side_effect=transport)
    )
    monkeypatch.setattr(tracking, "send_internal_email", adapter)
    if outcome == "sent":
        result = await tracking.escalate(pool, 7, USER, "Missing <permit>")
        assert result["email_sent_at"] == stamp
    else:
        with pytest.raises(HTTPException) as error:
            await tracking.escalate(pool, 7, USER, "Missing <permit>")
        assert error.value.status_code == 502
    args = adapter.await_args.kwargs
    assert args["to"] == "zero@balizero.com" and args["raise_on_failure"]
    assert args["idempotency_key"] == str(claimed["email_idempotency_key"])
    assert "&lt;permit&gt;" in args["body"] and "<permit>" not in args["body"]
    assert conn.execute.await_args.args[-1] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sent", "sending", "uncertain", "fulfilled"])
async def test_reading_and_replaying_a_claim_never_sends_again(db, monkeypatch, status):
    pool, _ = db
    stamp = datetime.now(UTC)
    already = row(
        email_state=status if status != "fulfilled" else "not_sent",
        email_last_attempt_at=stamp,
        email_sent_at=stamp if status == "sent" else None,
        fulfilled_at=stamp if status == "fulfilled" else None,
    )
    monkeypatch.setattr(tracking, "business_today", lambda: DAY0 + timedelta(days=66))
    monkeypatch.setattr(tracking, "_authorized_record", AsyncMock(return_value=already))
    sender = AsyncMock()
    monkeypatch.setattr(tracking, "send_internal_email", sender)
    if status == "sent":
        result = await tracking.escalate(pool, 7, USER, "Explanation")
        assert result["email_sent_at"] == stamp
    else:
        with pytest.raises(HTTPException):
            await tracking.escalate(pool, 7, USER, "Explanation")
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_issuance_cannot_start_a_clock_without_its_durable_receipt(monkeypatch):
    monkeypatch.setattr(
        tracking, "delivery_policy", lambda _: {"followup_slot": "document_2", "can_postpone": True}
    )
    conn = AsyncMock()
    conn.fetchval.return_value = False
    with pytest.raises(HTTPException, match="receipt is missing"):
        await tracking.record_visa_milestone(
            conn, {"id": 7, "code": "visa_c1_tourism"}, "electronic_visa"
        )
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_consultant_cannot_attest_to_owner_receipt(db, monkeypatch):
    allow = AsyncMock()
    monkeypatch.setattr(tracking, "_authorized_record", allow)
    with pytest.raises(HTTPException) as denied:
        await tracking.confirm_receipt(db[0], 7, USER, "mailbox-message-1")
    assert denied.value.status_code == 403
    allow.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_confirmation_preserves_ambiguous_attempt_and_pay_hold(db, monkeypatch):
    pool, conn = db
    owner = {"email": "zero@balizero.com", "role": "admin"}
    stamp = datetime.now(UTC)
    prior = row(email_state="uncertain", email_attempts=1, escalation_note="Client delayed")
    final = {
        **prior,
        "email_state": "sent",
        "email_sent_at": stamp,
        "email_receipt_confirmed_at": stamp,
        "email_receipt_confirmed_by": owner["email"],
        "email_receipt_reference": "mailbox-message-1",
    }
    allowed = AsyncMock(side_effect=[prior, final, final])
    conn.fetchrow.return_value = final
    sender = AsyncMock()
    monkeypatch.setattr(tracking, "_authorized_record", allowed)
    monkeypatch.setattr(tracking, "send_internal_email", sender)
    view = await tracking.confirm_receipt(pool, 7, owner, "mailbox-message-1")
    assert view["email_receipt_confirmed_at"] == stamp and view["status"] == "email_sent_permit_due"
    assert not view["can_confirm_receipt"] and view["fulfilled_at"] is None
    assert await tracking.confirm_receipt(pool, 7, owner, "mailbox-message-1") == view
    with pytest.raises(HTTPException):
        await tracking.confirm_receipt(pool, 7, owner, "a-different-reference")
    assert conn.fetchrow.await_count == 1
    sender.assert_not_awaited()


def test_only_owner_can_confirm_uncertain_or_abandoned_send():
    owner = {"email": "zero@balizero.com", "role": "admin"}
    now = datetime.now(UTC)
    stale = row(
        email_state="sending", email_attempts=1, email_last_attempt_at=now - timedelta(minutes=10)
    )
    assert tracking.followup_view(stale, DAY0, user=owner, now=now)["can_confirm_receipt"]
    assert not tracking.followup_view(stale, DAY0, user=USER, now=now)["can_confirm_receipt"]
    assert not tracking.followup_view(
        row(email_state="failed", email_attempts=1), DAY0, user=owner
    )["can_confirm_receipt"]
