"""The invoice flow, with the real generator, on a buyer name the invoice fonts cannot print.

The generator refuses; the flow reports a failed PDF with the reason, sends nothing and
stores no invoice. A name the fonts lack only in accents goes through.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.invoice_generator import InvoiceGenerator
from backend.tests.unit.services.invoicing.test_invoice_round2_accounting_mail import (  # noqa: F401
    D_KEY,
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

CYRILLIC = "\u0421\u044a\u0435\u0448\u044c \u0415\u0449\u0451"
POLISH = "Za\u017c\u00f3\u0142\u0107 G\u0119\u015bl\u0105 Ja\u017a\u0144"


def _flow(service, full_name: str):
    conn = service.db_pool._conn
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice_data(practice_type_code="tax_monthly_basic_0_50"),
            client_data(full_name=full_name),
            lookup_row(db_now=POST_CUTOVER),
            practice_row(),
            binding_row(D_KEY, POST_CUTOVER),
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_a_refused_buyer_name_is_a_failed_pdf_with_its_reason_and_nothing_is_sent(
    env, synth, monkeypatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, email_client = env
    service.invoice_generator = InvoiceGenerator()
    conn = _flow(service, CYRILLIC)

    result = await service.trigger_on_sending_invoice(1, "agent@example.test")

    assert result == {
        "success": False,
        "error": (
            "PDF generation failed: the buyer name has 8 letter(s) the invoice fonts cannot "
            "print (CYRILLIC); write it in basic Latin letters on the record and generate the "
            "invoice again"
        ),
    }
    email_client.post.assert_not_called()
    stored = [call for call in conn.execute.call_args_list if "invoices" in str(call.args[0])]
    assert stored == []


@pytest.mark.asyncio
async def test_a_buyer_name_with_accents_the_fonts_lack_goes_through(
    env, synth, monkeypatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, email_client = env
    service.invoice_generator = InvoiceGenerator()
    _flow(service, POLISH)

    result = await service.trigger_on_sending_invoice(1, "agent@example.test")

    assert result["success"] is True
    assert result["email_sent"] is True
