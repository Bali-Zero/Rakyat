"""D4b T6: the trigger result carries email_attempted so callers can tell no_email from failed.

-> test_trigger_result_carries_email_attempted (three outcomes through the full flow)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.client_email_outcome import NO_EMAIL, SENT, client_email_outcome
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "client_mail_raises", "attempted", "sent"),
    [
        ({}, False, True, True),
        ({}, True, True, False),
        ({"email": None}, False, False, False),
    ],
    ids=["sent", "failed", "no_email"],
)
async def test_trigger_result_carries_email_attempted(
    env, synth, monkeypatch, overrides, client_mail_raises, attempted, sent
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, email_client = env
    conn = service.db_pool._conn
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice_data(practice_type_code="tax_monthly_basic_0_50"),
            client_data(**overrides),
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
    assert result["email_attempted"] is attempted
    assert result["email_sent"] is sent
    if not attempted:
        assert client_email_outcome(result["email_sent"], result["email_attempted"]) == NO_EMAIL
    if sent:
        assert client_email_outcome(result["email_sent"], result["email_attempted"]) == SENT
