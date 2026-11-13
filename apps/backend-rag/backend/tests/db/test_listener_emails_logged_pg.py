"""Every email the practice-status listener sends is audited in ``email_send_log`` (D1).

Real Postgres with the prod-shaped schema of ``test_payment_emails_once_pg`` (``email_send_log`` is
migration 126, verbatim constraints). Placeholders only: no client data. Requires the
mandate-owned disposable PostgreSQL (``NUZ_UNIVERSAL_PG_SYSTEM_ID`` / ``NUZ_UNIVERSAL_PG_DSN``);
skipped otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import pytest

from backend.tests.db.test_payment_emails_once_pg import (  # noqa: F401  (fixtures)
    CLIENT,
    TEAM,
    _listener,
    _paid,
    _payload,
    _seed,
    pool,
    posts,
)


async def _status_change(listener: Any, pid: int, new_status: str) -> None:
    await listener._handle_notification(
        _payload(
            pid,
            old_status="on_process",
            new_status=new_status,
            old_payment="paid",
            new_payment="paid",
        )
    )


async def _rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT email_type, status, provider, to_email, practice_id, client_id, error_message,"
        " retry_after FROM email_send_log ORDER BY id"
    )


@pytest.mark.asyncio
async def test_the_completed_mail_leaves_a_sent_row(pool, posts):
    """D1-1: the Completed client mail (the Bites of this change) is in email_send_log."""
    pid = await _seed(pool, payment="paid")
    await _status_change(_listener(pool), pid, "completed")
    rows = await _rows(pool)
    assert [(r["email_type"], r["status"], r["provider"]) for r in rows] == [
        ("completion_client", "sent", "brevo")
    ]
    assert rows[0]["to_email"] == CLIENT and rows[0]["practice_id"] == pid
    assert rows[0]["client_id"] is not None
    assert len(posts.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "email_type"),
    [("submitted", "status_submitted_client"), ("approved", "status_approved_client")],
)
async def test_the_other_milestone_mails_are_logged_with_their_own_type(
    pool, posts, status, email_type
):
    """D1-2."""
    pid = await _seed(pool, payment="paid")
    await _status_change(_listener(pool), pid, status)
    assert [(r["email_type"], r["status"]) for r in await _rows(pool)] == [(email_type, "sent")]


@pytest.mark.asyncio
async def test_the_payment_mails_are_logged(pool, posts):
    """D1-3: the thank-you and the team mail each leave a row, sent via brevo."""
    pid = await _seed(pool)
    await _paid(_listener(pool), pid)
    rows = await _rows(pool)
    assert [(r["email_type"], r["status"], r["provider"]) for r in rows] == [
        ("payment_client", "sent", "brevo"),
        ("payment_team", "sent", "brevo"),
    ]
    assert {r["to_email"] for r in rows} == {CLIENT, TEAM}


@pytest.mark.asyncio
async def test_a_failed_client_send_is_a_failed_row_that_is_never_retried_as_a_stub(pool, posts):
    """D1-4: failure leaves status failed with the error and NO retry_after (client types are
    non-resurrectable: a "[RETRY]" stub must never reach a client); nothing escapes."""
    pid = await _seed(pool, payment="paid")
    posts.fail = True
    await _status_change(_listener(pool), pid, "completed")
    [row] = await _rows(pool)
    assert row["status"] == "failed" and row["provider"] == "brevo"
    assert row["error_message"] and row["retry_after"] is None


@pytest.mark.asyncio
async def test_a_failed_team_payment_mail_stays_retryable_but_the_client_one_does_not(pool, posts):
    """D1-5: innocence for the retry policy of the types."""
    pid = await _seed(pool)
    posts.fail = True
    await _paid(_listener(pool), pid)
    by_type = {r["email_type"]: r for r in await _rows(pool)}
    assert by_type["payment_client"]["status"] == "failed"
    assert by_type["payment_client"]["retry_after"] is None
    assert by_type["payment_team"]["status"] == "failed"
    assert by_type["payment_team"]["retry_after"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_no_listener_log_record_carries_the_recipient_address(pool, posts, caplog, fail):
    """D1-6: the sent and the failed paths log without any recipient address."""
    pid = await _seed(pool, payment="paid")
    posts.fail = fail
    with caplog.at_level(logging.DEBUG):
        await _status_change(_listener(pool), pid, "completed")
        await _paid(_listener(pool), pid, old="unpaid")
    text = "\n".join(
        f"{r.getMessage()} {r.exc_text or ''} {r.exc_info[1] if r.exc_info else ''}"
        for r in caplog.records
        if r.name.startswith("backend.")
    )
    assert "practice_status_listener" in {r.name.rsplit(".", 1)[-1] for r in caplog.records}
    assert CLIENT not in text and TEAM not in text


@pytest.mark.asyncio
async def test_on_process_is_still_logged_once_by_its_own_sender(pool, posts):
    """D1-7: innocence: the On Process start mails keep their own rows, the listener adds none."""
    pid = await _seed(pool, payment="unpaid")
    await _listener(pool)._handle_notification(
        _payload(
            pid,
            old_status="sending_invoice",
            new_status="on_process",
            old_payment="unpaid",
            new_payment="unpaid",
        )
    )
    assert [r["email_type"] for r in await _rows(pool)] == [
        "process_start_client",
        "process_start_team",
    ]
