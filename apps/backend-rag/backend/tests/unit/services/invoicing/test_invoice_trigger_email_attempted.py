"""D4b T6/T7.

T6 the trigger result carries email_attempted so callers can tell no_email from failed
   -> test_trigger_result_carries_email_attempted (three outcomes through the full flow)
T7 the client's timeline ends on the real outcome: a client-visible "Invoice sent" + invoice number
   is written ONLY after the client mail went out; nothing client-visible on no_email or failed;
   nothing when the practice is hidden from the client; a failing write never fails the flow
   -> test_timeline_gets_invoice_sent_only_after_a_real_send,
      test_timeline_write_is_skipped_for_a_hidden_practice,
      test_a_failing_timeline_write_does_not_fail_the_invoice_flow
T8 the team-visible activity log line says "created and sent" only when the client mail went out;
   on no_email / failed it says the client was NOT emailed
   -> test_activity_log_line_matches_the_real_outcome (sent / failed / no_email)
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


def timeline_calls(conn) -> list:
    return [c for c in conn.execute.await_args_list if "timeline_events" in str(c.args[0])]


async def run_flow(env, monkeypatch, *, overrides=None, practice_overrides=None, mail_raises=False):
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, email_client = env
    conn = service.db_pool._conn
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice_data(
                practice_type_code="tax_monthly_basic_0_50", **(practice_overrides or {})
            ),
            client_data(**(overrides or {})),
            lookup_row(db_now=POST_CUTOVER),
            practice_row(),
            binding_row(D_KEY, POST_CUTOVER),
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    if mail_raises:
        email_client.post = AsyncMock(side_effect=[RuntimeError("synthetic send failure"), ok])
    result = await service.trigger_on_sending_invoice(1, "agent@example.test")
    return result, conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "mail_raises", "written"),
    [({}, False, True), ({}, True, False), ({"email": None}, False, False)],
    ids=["sent", "failed", "no_email"],
)
async def test_timeline_gets_invoice_sent_only_after_a_real_send(
    env, synth, monkeypatch, overrides, mail_raises, written
) -> None:
    result, conn = await run_flow(env, monkeypatch, overrides=overrides, mail_raises=mail_raises)
    assert result["success"] is True
    calls = timeline_calls(conn)
    if written:
        assert len(calls) == 1
        sql, *args = calls[0].args
        assert "'Invoice sent'" in sql and "true" in sql
        assert args == [100, 1, f"Invoice {result['invoice_number']}", "agent@example.test"]
    else:
        assert calls == []


@pytest.mark.asyncio
async def test_timeline_write_is_skipped_for_a_hidden_practice(env, synth, monkeypatch) -> None:
    result, conn = await run_flow(env, monkeypatch, practice_overrides={"client_visible": False})
    assert result["success"] is True and result["email_sent"] is True
    assert timeline_calls(conn) == []


@pytest.mark.asyncio
async def test_a_failing_timeline_write_does_not_fail_the_invoice_flow(
    env, synth, monkeypatch
) -> None:
    class MissingTable(Exception):
        sqlstate = "42P01"

    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    service, _, _ = env
    conn = service.db_pool._conn
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice_data(practice_type_code="tax_monthly_basic_0_50"),
            client_data(),
            lookup_row(db_now=POST_CUTOVER),
            practice_row(),
            binding_row(D_KEY, POST_CUTOVER),
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)

    async def execute(sql, *args):
        if "timeline_events" in sql:
            raise MissingTable()

    conn.execute = AsyncMock(side_effect=execute)
    result = await service.trigger_on_sending_invoice(1, "agent@example.test")
    assert result["success"] is True
    assert result["email_sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "mail_raises", "expected"),
    [
        ({}, False, "created and sent"),
        ({}, True, "the email to the client FAILED, client NOT emailed"),
        ({"email": None}, False, "NO email on file, client NOT emailed"),
    ],
    ids=["sent", "failed", "no_email"],
)
async def test_activity_log_line_matches_the_real_outcome(
    env, synth, monkeypatch, overrides, mail_raises, expected
) -> None:
    result, conn = await run_flow(env, monkeypatch, overrides=overrides, mail_raises=mail_raises)
    assert result["success"] is True
    rows = [c.args for c in conn.execute.await_args_list if "activity_log" in str(c.args[0])]
    assert len(rows) == 1
    description = rows[0][-1]
    assert description.startswith(f"Invoice {result['invoice_number']}")
    assert expected in description
    if expected != "created and sent":
        assert "created and sent" not in description
