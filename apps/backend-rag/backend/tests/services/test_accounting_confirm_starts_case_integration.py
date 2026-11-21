"""Real-PostgreSQL tests of owner rule (a): a payment confirmed in full starts the case.

A case in sending_invoice whose payment is confirmed in full moves to on_process in the same
statement, with the client timeline entry and the activity row the CRM writes when the move is
made by hand. Nothing else moves a case: a partial confirmation, any other status, a historical
confirmation. All names and amounts are invented.
"""

from __future__ import annotations

from datetime import date

import asyncpg
import pytest

from backend.services.accounting.reconcile_service import (
    START_TIMELINE_EVENT,
    ReconcileError,
    confirm_payment,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    DEFAULT_COMPANY_STATEMENT_SQL,
    _drop_db,
    _fresh_db,
)

WHO = "accountant@example.test"
DAY = date(2026, 6, 25)


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


async def _case(
    conn,
    status: str | None,
    *,
    client: bool = True,
    visible: bool | None = True,
    summary: str | None = None,
    start: date | None = None,
) -> tuple[int, int, int]:
    """A case in `status` with one 4,000,000 invoice and one 4,000,000 transfer for it."""
    cid = None
    if client:
        cid = await conn.fetchval(
            "INSERT INTO clients (full_name) VALUES ('Contoh Klien') RETURNING id"
        )
    pid = await conn.fetchval(
        "INSERT INTO practices (client_id, status, client_visible, client_summary, start_date)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        cid,
        status,
        visible,
        summary,
        start,
    )
    iid = await conn.fetchval(
        "INSERT INTO invoices (practice_id, invoice_number, amount_idr)"
        " VALUES ($1, 'INV-S', 4000000) RETURNING id",
        pid,
    )
    sid = await conn.fetchval("SELECT id FROM bank_statements ORDER BY id LIMIT 1")
    if sid is None:
        sid = await conn.fetchval(DEFAULT_COMPANY_STATEMENT_SQL)
    txid = await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction)"
        " VALUES ($1, $2, 'TRF Contoh Klien', 4000000, 'credit') RETURNING id",
        sid,
        DAY,
    )
    return pid, iid, txid


def _confirm(conn, pid, iid, txid, *, status="paid", amount=4_000_000, historical=False):
    return confirm_payment(
        conn,
        bank_txn_id=txid,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=amount,
        new_status=status,
        confirmed_by=WHO,
        movement_date=DAY,
        historical=historical,
    )


async def _state(conn, pid):
    return await conn.fetchrow(
        "SELECT status, start_date, payment_status FROM practices WHERE id = $1", pid
    )


async def _side_rows(conn):
    timeline = [dict(r) for r in await conn.fetch("SELECT * FROM timeline_events ORDER BY id")]
    activity = [dict(r) for r in await conn.fetch("SELECT * FROM activity_log ORDER BY id")]
    return timeline, activity


async def test_a_full_confirmation_starts_a_case_waiting_for_payment(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice", summary="Visa contoh")
    cid = await db.fetchval("SELECT client_id FROM practices WHERE id = $1", pid)
    today = await db.fetchval("SELECT now()::date")

    await _confirm(db, pid, iid, txid)

    state = await _state(db, pid)
    assert (state["status"], state["start_date"], state["payment_status"]) == (
        "on_process",
        today,
        "paid",
    )
    timeline, activity = await _side_rows(db)
    assert len(timeline) == 1
    entry = timeline[0]
    assert entry["event_date"] is not None and entry["created_at"] is not None
    assert {k: v for k, v in entry.items() if k not in ("id", "event_date", "created_at")} == {
        "client_id": cid,
        "practice_id": pid,
        "event_type": "milestone",
        "title": "Application in process",
        "description": "Visa contoh",
        "client_visible": True,
        "color": "info",
        "created_by": WHO,
    }
    assert START_TIMELINE_EVENT == ("milestone", "Application in process", "info")
    assert len(activity) == 1
    row = activity[0]
    assert row["performed_at"] is not None
    assert {k: v for k, v in row.items() if k not in ("id", "performed_at")} == {
        "entity_type": "practice",
        "entity_id": pid,
        "action": "updated",
        "performed_by": WHO,
        "changes": {"status": "on_process"},
        "description": "Updated: status",
    }


async def test_a_start_date_already_set_is_kept_and_no_summary_reads_the_status(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice", start=date(2026, 6, 1))
    await _confirm(db, pid, iid, txid)
    assert (await _state(db, pid))["start_date"] == date(2026, 6, 1)
    timeline, _ = await _side_rows(db)
    assert [t["description"] for t in timeline] == ["Status: on_process"]


@pytest.mark.parametrize(
    "status", ["inquiry", "waiting_documents", "on_process", "completed", "cancelled", None]
)
async def test_a_full_confirmation_moves_no_case_in_any_other_status(db, status) -> None:
    pid, iid, txid = await _case(db, status)
    await _confirm(db, pid, iid, txid)
    state = await _state(db, pid)
    assert (state["status"], state["start_date"], state["payment_status"]) == (status, None, "paid")
    assert await _side_rows(db) == ([], [])


async def test_a_partial_confirmation_does_not_start_the_case(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid, status="partial", amount=2_000_000)
    state = await _state(db, pid)
    assert (state["status"], state["start_date"], state["payment_status"]) == (
        "sending_invoice",
        None,
        "partial",
    )
    assert await _side_rows(db) == ([], [])


async def test_a_historical_confirmation_never_moves_the_case(db) -> None:
    """The listener tells a historical change by its unchanged status: moving the case would
    email the client about an old payment."""
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid, historical=True)
    state = await _state(db, pid)
    assert (state["status"], state["start_date"], state["payment_status"]) == (
        "sending_invoice",
        None,
        "paid",
    )
    assert await _side_rows(db) == ([], [])


@pytest.mark.parametrize(("client", "visible"), [(True, False), (False, True)])
async def test_a_case_hidden_from_its_client_or_without_one_gets_no_timeline_entry(
    db, client, visible
) -> None:
    pid, iid, txid = await _case(db, "sending_invoice", client=client, visible=visible)
    await _confirm(db, pid, iid, txid)
    assert (await _state(db, pid))["status"] == "on_process"
    timeline, activity = await _side_rows(db)
    assert timeline == [] and len(activity) == 1


async def test_a_null_visibility_writes_no_timeline_entry_as_the_crm_does(db) -> None:
    """The CRM's manual move reads bool(client_visible): NULL hides the entry there too."""
    pid, iid, txid = await _case(db, "sending_invoice", visible=None)
    await _confirm(db, pid, iid, txid)
    assert (await _state(db, pid))["status"] == "on_process"
    timeline, activity = await _side_rows(db)
    assert timeline == [] and len(activity) == 1


async def test_without_a_timeline_table_the_case_still_starts(db) -> None:
    await db.execute("DROP TABLE timeline_events")
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid)
    assert (await _state(db, pid))["status"] == "on_process"
    assert len(await db.fetch("SELECT id FROM activity_log")) == 1


async def test_another_timeline_failure_rolls_the_whole_confirmation_back(db) -> None:
    await db.execute(
        "CREATE FUNCTION fail_timeline() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'timeline refused'; END $$"
    )
    await db.execute(
        "CREATE TRIGGER fail_timeline BEFORE INSERT ON timeline_events FOR EACH ROW"
        " EXECUTE FUNCTION fail_timeline()"
    )
    pid, iid, txid = await _case(db, "sending_invoice")
    with pytest.raises(asyncpg.RaiseError):
        await _confirm(db, pid, iid, txid)
    state = await _state(db, pid)
    assert (state["status"], state["payment_status"]) == ("sending_invoice", "unpaid")
    assert await db.fetchval("SELECT count(*) FROM weekly_cashout") == 0
    assert await db.fetchval("SELECT count(*) FROM reconciliation_log") == 0
    assert (
        await db.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid)
        == "unmatched"
    )


async def test_an_activity_failure_rolls_the_whole_confirmation_back(db) -> None:
    await db.execute("DROP TABLE activity_log")
    pid, iid, txid = await _case(db, "sending_invoice")
    with pytest.raises(asyncpg.UndefinedTableError):
        await _confirm(db, pid, iid, txid)
    state = await _state(db, pid)
    assert (state["status"], state["payment_status"]) == ("sending_invoice", "unpaid")
    assert await db.fetchval("SELECT count(*) FROM timeline_events") == 0
    assert await db.fetchval("SELECT count(*) FROM weekly_cashout") == 0


async def test_a_refused_confirmation_starts_nothing(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice")
    other_pid, _, _ = await _case(db, "sending_invoice")
    with pytest.raises(ReconcileError):
        await _confirm(db, other_pid, iid, txid)  # the invoice belongs to another case
    for p in (pid, other_pid):
        assert (await _state(db, p))["status"] == "sending_invoice"
    assert await _side_rows(db) == ([], [])


async def test_the_start_and_the_payment_reach_the_listener_as_one_change(db) -> None:
    """One UPDATE writes both, so a row trigger sees payment and status move together: the
    listener then sends the payment emails and skips the start emails (case already paid)."""
    await db.execute(
        "CREATE TABLE changes (old_status text, new_status text, old_payment text, new_payment text)"
    )
    await db.execute(
        "CREATE FUNCTION record_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
        " INSERT INTO changes VALUES (OLD.status, NEW.status, OLD.payment_status, NEW.payment_status);"
        " RETURN NEW; END $$"
    )
    await db.execute(
        "CREATE TRIGGER record_change AFTER UPDATE ON practices FOR EACH ROW"
        " EXECUTE FUNCTION record_change()"
    )
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid)
    rows = [tuple(r) for r in await db.fetch("SELECT * FROM changes")]
    assert rows == [("sending_invoice", "on_process", "unpaid", "paid")]
