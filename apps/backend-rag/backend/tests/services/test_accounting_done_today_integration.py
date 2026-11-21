"""Real-PostgreSQL tests of the Done today statement (spec 1a, B3 B4 B5 B6 B8).

The rows are written by the real writers (confirm_payment, link_recorded, ignore_transaction,
reopen_transaction); the instants are then fixed so a run cannot straddle a WITA midnight.
All names and amounts are invented.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from backend.services.accounting.done_today_service import load_done_today
from backend.services.accounting.reconcile_service import (
    confirm_payment,
    ignore_transaction,
    link_recorded,
    reopen_transaction,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    DEFAULT_COMPANY_STATEMENT_SQL,
    _drop_db,
    _fresh_db,
)

WHO = "SENTINEL-WHO@example.test"
TABLES = (
    "clients",
    "practices",
    "invoices",
    "bank_statements",
    "bank_transactions",
    "weekly_cashout",
    "reconciliation_log",
)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=ZoneInfo("Asia/Makassar"))


def _at(day: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=UTC)


# One window: [2026-11-17T16:00Z, 2026-11-18T16:00Z)
IN_WINDOW = _at(18, 3)
BEFORE_WINDOW = _at(17, 15, 59, 59)
AFTER_WINDOW = _at(18, 16)


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


async def _practice(conn, *, paid: bool, client: str | None = "PT CONTOH SATU", invoice=True):
    cid = (
        await conn.fetchval("INSERT INTO clients (full_name) VALUES ($1) RETURNING id", client)
        if client is not None
        else None
    )
    pid = await conn.fetchval(
        "INSERT INTO practices (client_id, payment_status) VALUES ($1, $2) RETURNING id",
        cid,
        "paid" if paid else "unpaid",
    )
    iid = None
    if invoice:
        iid = await conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr)"
            " VALUES ($1, $2, 4000000) RETURNING id",
            pid,
            f"INV-000000-{pid:05d}",
        )
    return pid, iid


async def _bank(conn, *, direction="credit") -> int:
    sid = await conn.fetchval("SELECT id FROM bank_statements ORDER BY id LIMIT 1")
    if sid is None:
        sid = await conn.fetchval(DEFAULT_COMPANY_STATEMENT_SQL)
    return await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction)"
        " VALUES ($1, $2, 'TRF PT CONTOH SATU', 4000000, $3) RETURNING id",
        sid,
        date(2026, 11, 17),
        direction,
    )


async def _confirm(conn, txid, pid, iid) -> int:
    res = await confirm_payment(
        conn,
        bank_txn_id=txid,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
    )
    return res.reconciliation_log_id


async def _link(conn, txid) -> int:
    pid, iid = await _practice(conn, paid=True)
    res = await link_recorded(
        conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO
    )
    return res.reconciliation_log_id


async def _ignore(conn, txid, reason="SENTINEL-REASON") -> int:
    res = await ignore_transaction(conn, bank_txn_id=txid, reason=reason, ignored_by=WHO)
    return res.reconciliation_log_id


async def _reopen(conn, txid) -> int:
    return (await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)).reconciliation_log_id


async def _stamp(conn, log_id: int, when: datetime) -> None:
    await conn.execute(
        "UPDATE reconciliation_log SET confirmed_at = $1 WHERE id = $2", when, log_id
    )


async def _full(conn) -> dict[str, list[dict]]:
    return {
        t: [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")] for t in TABLES
    }


async def _forbid_all_writes(conn) -> None:
    await conn.execute(
        "CREATE FUNCTION forbid_write() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'table written'; END $$"
    )
    for t in TABLES:
        await conn.execute(
            f"CREATE TRIGGER no_write_{t} BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {t}"
            " FOR EACH STATEMENT EXECUTE FUNCTION forbid_write()"
        )


def _ids(body) -> list[int]:
    return [i["txn_id"] for i in body["items"]]


async def test_the_seven_transfers_give_exactly_a_b_c_e(db) -> None:
    conn = db
    a, b, c, d, e = [await _bank(conn) for _ in range(5)]
    g = await _bank(conn, direction="debit")
    pa, ia = await _practice(conn, paid=False)
    a_log = await _confirm(conn, a, pa, ia)
    b_log = await _link(conn, b)
    c_log = await _ignore(conn, c)
    d_ignore = await _ignore(conn, d)
    d_reopen = await _reopen(conn, d)
    e_link = await _link(conn, e)
    e_reopen = await _reopen(conn, e)
    e_ignore = await _ignore(conn, e)
    pf, _ = await _practice(conn, paid=False, invoice=False)
    f_log = await _confirm(conn, None, pf, None)
    g_log = await _ignore(conn, g)

    for log_id, when in (
        (a_log, _at(18, 3)),
        (b_log, _at(18, 4)),
        (c_log, _at(18, 5)),
        (e_link, _at(18, 6)),
        (e_reopen, _at(18, 7)),
        (e_ignore, _at(18, 8)),
        (d_ignore, _at(18, 9)),
        (d_reopen, _at(18, 10)),
        (f_log, _at(18, 11)),
        (g_log, _at(18, 12)),
    ):
        await _stamp(conn, log_id, when)

    await _forbid_all_writes(conn)
    before = await _full(conn)
    body = await load_done_today(conn, NOW)
    assert await _full(conn) == before

    assert body["day"] == "2026-11-18"
    assert body["counts"] == {"confirmed": 1, "linked": 1, "ignored": 2, "total": 4}
    assert body["truncated"] is False
    assert [(i["txn_id"], i["action"]) for i in body["items"]] == [
        (e, "ignored"),
        (c, "ignored"),
        (b, "linked"),
        (a, "confirmed"),
    ]
    by_id = {i["txn_id"]: i for i in body["items"]}
    # E has three rows; it appears once, with the time of its newest row
    assert by_id[e]["done_at"] == "2026-11-18T16:00:00+08:00"
    assert by_id[a]["invoice_number"] == f"INV-000000-{pa:05d}"
    assert by_id[a]["client_name"] == "PT CONTOH SATU"
    assert by_id[b]["invoice_number"].startswith("INV-000000-")
    assert by_id[b]["client_name"] == "PT CONTOH SATU"
    for ignored in (c, e):
        assert by_id[ignored]["invoice_number"] is None
        assert by_id[ignored]["client_name"] is None
    assert by_id[a]["txn_date"] == "2026-11-17"
    assert by_id[a]["amount_idr"] == 4_000_000
    assert by_id[a]["description"] == "TRF PT CONTOH SATU"


async def test_b5_the_ignore_reason_and_the_actor_never_leave(db) -> None:
    conn = db
    txid = await _bank(conn)
    await _stamp(conn, await _ignore(conn, txid, "SENTINEL-REASON-ONE"), IN_WINDOW)
    body = await load_done_today(conn, NOW)
    assert _ids(body) == [txid]
    dumped = json.dumps(body)
    assert "SENTINEL" not in dumped
    assert set(body["items"][0]) == {
        "txn_id",
        "action",
        "done_at",
        "txn_date",
        "amount_idr",
        "description",
        "invoice_number",
        "client_name",
    }


async def test_b8_the_four_edge_instants_land_on_the_right_side(db) -> None:
    conn = db
    ids = {}
    for name, when in (
        ("just_before", _at(17, 15, 59, 59)),
        ("start", _at(17, 16)),
        ("last_second", _at(18, 15, 59, 59)),
        ("end", _at(18, 16)),
    ):
        ids[name] = await _bank(conn)
        await _stamp(conn, await _ignore(conn, ids[name]), when)
    body = await load_done_today(conn, NOW)
    assert _ids(body) == [ids["last_second"], ids["start"]]
    assert body["counts"]["total"] == 2


async def test_a_higher_id_row_outside_the_window_hides_the_transfer(db) -> None:
    conn = db
    after, before, inside = await _bank(conn), await _bank(conn), await _bank(conn)
    for txid, last in ((after, AFTER_WINDOW), (before, BEFORE_WINDOW)):
        first = await _ignore(conn, txid)
        reopened = await _reopen(conn, txid)
        newest = await _ignore(conn, txid)
        await _stamp(conn, first, IN_WINDOW)
        await _stamp(conn, reopened, IN_WINDOW)
        await _stamp(conn, newest, last)
    await _stamp(conn, await _ignore(conn, inside), IN_WINDOW)
    body = await load_done_today(conn, NOW)
    assert _ids(body) == [inside]
    assert body["counts"]["total"] == 1


async def test_equal_instants_are_ordered_by_higher_txn_id_first(db) -> None:
    conn = db
    a, b, c = [await _bank(conn) for _ in range(3)]
    # log rows written b, a, c: neither log-id order nor insertion order is txn-id order
    for txid in (b, a, c):
        await _stamp(conn, await _ignore(conn, txid), IN_WINDOW)
    body = await load_done_today(conn, NOW)
    assert _ids(body) == [c, b, a]


async def test_two_transfers_in_one_second_are_ordered_by_the_stored_instant(db) -> None:
    conn = db
    lower, higher = await _bank(conn), await _bank(conn)
    await _stamp(
        conn, await _ignore(conn, lower), datetime(2026, 11, 18, 3, 0, 0, 700000, tzinfo=UTC)
    )
    await _stamp(
        conn, await _ignore(conn, higher), datetime(2026, 11, 18, 3, 0, 0, 200000, tzinfo=UTC)
    )
    body = await load_done_today(conn, NOW)
    assert _ids(body) == [lower, higher]
    assert body["items"][0]["done_at"] == body["items"][1]["done_at"] == "2026-11-18T11:00:00+08:00"


async def test_a_missing_invoice_or_a_practice_without_client_gives_nulls_not_a_dropped_row(
    db,
) -> None:
    conn = db
    no_invoice, gone_invoice, no_client = await _bank(conn), await _bank(conn), await _bank(conn)

    # A confirmation that names no invoice. Its practice has one: a transfer settles only
    # invoices of the company whose account it reached, so a practice with none is refused.
    p1, _ = await _practice(conn, paid=False)
    await _stamp(conn, await _confirm(conn, no_invoice, p1, None), IN_WINDOW)

    p2, i2 = await _practice(conn, paid=False)
    await _stamp(conn, await _confirm(conn, gone_invoice, p2, i2), IN_WINDOW)
    await conn.execute("DELETE FROM invoices WHERE id = $1", i2)

    p3, i3 = await _practice(conn, paid=False, client=None)
    await _stamp(conn, await _confirm(conn, no_client, p3, i3), IN_WINDOW)

    body = await load_done_today(conn, NOW)
    by_id = {i["txn_id"]: i for i in body["items"]}
    assert set(by_id) == {no_invoice, gone_invoice, no_client}
    assert by_id[no_invoice]["invoice_number"] is None
    assert by_id[no_invoice]["client_name"] == "PT CONTOH SATU"
    assert by_id[gone_invoice]["invoice_number"] is None
    assert by_id[gone_invoice]["client_name"] == "PT CONTOH SATU"
    assert by_id[no_client]["invoice_number"] == f"INV-000000-{p3:05d}"
    assert by_id[no_client]["client_name"] is None


async def test_a_confirmation_without_a_bank_transaction_is_not_counted(db) -> None:
    conn = db
    pid, _ = await _practice(conn, paid=False, invoice=False)
    await _stamp(conn, await _confirm(conn, None, pid, None), IN_WINDOW)
    body = await load_done_today(conn, NOW)
    assert body["items"] == []
    assert body["counts"] == {"confirmed": 0, "linked": 0, "ignored": 0, "total": 0}
