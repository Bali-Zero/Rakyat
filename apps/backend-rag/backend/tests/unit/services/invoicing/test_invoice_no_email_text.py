"""D4: the accounting mail never claims the client was emailed when they were not.

Spec rows (behaviour -> test), see the PR body:
S1 client emailed: text unchanged (innocence) -> test_client_emailed_text_is_unchanged
S2 no client email: NOT emailed, contact by WhatsApp/other channel, no "sent to" claim
   -> test_no_email_text_says_not_emailed_and_asks_to_contact
S3 client send FAILED: NOT received, send it yourself -> test_failed_send_text_says_not_received
S4 Email row marks a missing address (guilt) and shows a real one (innocence)
   -> test_email_row_marks_a_missing_address
S5 the trigger flow passes the real outcome (sent / failed / no_email)
   -> test_trigger_passes_the_real_client_email_outcome
Synthetic data only.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL
from backend.tests.unit.services.invoicing.test_invoice_round2_accounting_mail import (  # noqa: F401
    D_KEY,
    FINAL,
    POST_CUTOVER,
    binding_row,
    client_data,
    db_pool,
    env,
    lookup_row,
    practice_data,
    practice_row,
    synth,
)

PDF = b"%PDF-1.4 synthetic"


async def accounting_body(env, synth, **kwargs) -> str:
    service, _, email_client = env
    await service._send_accounting_notification(
        client_data=kwargs.pop("client", client_data()),
        practice_data=practice_data(),
        invoice_number="INV-D4-1",
        pdf_bytes=PDF,
        filename="Invoice_INV-D4-1.pdf",
        entity=synth[D_KEY],
        amount=FINAL,
        **kwargs,
    )
    return email_client.post.call_args.kwargs["json"]["body"]


@pytest.mark.asyncio
async def test_client_emailed_text_is_unchanged(env, synth) -> None:
    for kwargs in ({}, {"client_email_status": "sent"}):
        body = await accounting_body(env, synth, **kwargs)
        assert "generated and sent to Synthetic Client" in body
        assert "Reach out to the client via WhatsApp for payment follow-up" in body
        assert "NOT" not in body


@pytest.mark.asyncio
async def test_no_email_text_says_not_emailed_and_asks_to_contact(env, synth) -> None:
    body = await accounting_body(
        env, synth, client=client_data(email=None), client_email_status="no_email"
    )
    assert "NO email address on file" in body
    assert "NOT emailed" in body
    assert "WhatsApp or another channel" in body
    assert "sent to Synthetic Client" not in body
    assert "Reach out to the client via WhatsApp for payment follow-up" not in body


@pytest.mark.asyncio
async def test_failed_send_text_says_not_received(env, synth) -> None:
    body = await accounting_body(env, synth, client_email_status="failed")
    assert "FAILED" in body
    assert "NOT received it" in body
    assert "WhatsApp or another channel" in body
    assert "sent to Synthetic Client" not in body


@pytest.mark.asyncio
async def test_email_row_marks_a_missing_address(env, synth) -> None:
    missing = await accounting_body(
        env, synth, client=client_data(email=None), client_email_status="no_email"
    )
    assert "(no email on file)" in missing
    present = await accounting_body(env, synth)
    assert "client@example.test" in present
    assert "(no email on file)" not in present


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "client_overrides", "client_mail_raises", "expect"),
    [
        ("sent", {}, False, "generated and sent to Synthetic Client"),
        ("failed", {}, True, "FAILED"),
        ("no_email", {"email": None}, False, "NO email address on file"),
    ],
)
async def test_trigger_passes_the_real_client_email_outcome(
    env, synth, monkeypatch, case, client_overrides, client_mail_raises, expect
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, email_client = env
    conn = service.db_pool._conn
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice_data(practice_type_code="tax_monthly_basic_0_50"),
            client_data(**client_overrides),
            lookup_row(db_now=POST_CUTOVER),
            practice_row(),
            binding_row(D_KEY, POST_CUTOVER),
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    if client_mail_raises:
        email_client.post = AsyncMock(side_effect=[RuntimeError("synthetic send failure"), ok])
    result = await service.trigger_on_sending_invoice(1, "agent@example.test")

    assert result["success"] is True
    assert result["email_sent"] is (case == "sent")
    accounting = next(
        c.kwargs["json"]
        for c in email_client.post.await_args_list
        if c.kwargs["json"]["to"] == ACCOUNTING_EMAIL
    )
    assert expect in accounting["body"]
    if case != "sent":
        assert "sent to Synthetic Client" not in accounting["body"]
