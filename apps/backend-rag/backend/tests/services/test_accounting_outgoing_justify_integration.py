"""Real-PostgreSQL tests of justifying a bank debit (owner 2026-11-21/22). Synthetic data only."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from backend.services.accounting.outgoing_justify import (
    BATCH_LIMIT,
    OutgoingError,
    justify_outgoing,
    justify_pnbp_batch,
    undo_batch,
    undo_justification,
)
from backend.services.accounting.outgoings import load_outgoings
from backend.tests.services.test_accounting_outgoings_integration import (
    TRANSFER,
    _row,
    _statement,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    MIGRATION_375,
    MIGRATION_376,
    _drop_db,
    _forward_sql_of,
    _fresh_db,
)

AT = datetime(2026, 11, 21, 13, 19, 40)
WHO = "asya@example.test"


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        await conn.execute(_forward_sql_of(MIGRATION_375))
        await conn.execute(_forward_sql_of(MIGRATION_376))
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


async def _justify(db, txn_id, category="tax", **kw):
    args = {"practice_id": None, "recipient": None, "note": None, "lines": None}
    return await justify_outgoing(
        db, txn_id=txn_id, category=category, justified_by=WHO, **{**args, **kw}
    )


async def _refused(db, status, txn_id, category="tax", **kw) -> str:
    with pytest.raises(OutgoingError) as err:
        await _justify(db, txn_id, category, **kw)
    assert err.value.status == status
    return err.value.detail


async def _debit(db, kind="tax", amount=1_000_000):
    return await _row(db, await _statement(db), kind, amount, "x", AT)


def _line(amount, category="vendor_case", note="deed", practice_id=None):
    return {"amount_idr": amount, "category": category, "note": note, "practice_id": practice_id}


async def test_a_missing_or_credit_row_is_404_and_a_debit_is_justified(db) -> None:
    sid = await _statement(db)
    credit = await _row(db, sid, "client_credit", 5, "x", AT, direction="credit")
    await _refused(db, 404, 999_999)
    await _refused(db, 404, credit)
    debit = await _row(db, sid, "tax", 5, "x", AT)
    assert (await _justify(db, debit))["state"] == "justified"


async def test_double_justify_is_409_until_undone_then_it_can_be_done_again(db) -> None:
    txn = await _debit(db)
    await _justify(db, txn)
    await _refused(db, 409, txn)
    await undo_justification(db, txn_id=txn, undone_by=WHO)
    assert (await _justify(db, txn, "payroll"))["state"] == "justified"
    # Nothing was deleted: both rows are in the log, one of them undone.
    rows = await db.fetch("SELECT category, undone_at FROM outgoing_justifications ORDER BY id")
    assert [(r["category"], r["undone_at"] is not None) for r in rows] == [
        ("tax", True),
        ("payroll", False),
    ]


async def test_other_needs_a_note(db) -> None:
    txn = await _debit(db)
    await _refused(db, 422, txn, "other")
    await _refused(db, 422, txn, "other", note="   ")
    assert (await _justify(db, txn, "other", note="bank adjustment"))["state"] == "justified"


async def test_an_unknown_category_and_a_missing_case_are_422(db) -> None:
    txn = await _debit(db)
    await _refused(db, 422, txn, "gift")
    await _refused(db, 422, txn, "pnbp_case", practice_id=424242)


async def test_cash_withdrawal_needs_recipient_note_and_lines(db) -> None:
    txn = await _debit(db, "cash_withdrawal", 1_000_000)
    ok = {"recipient": "CONTOH PENARIK", "note": "cash for the week", "lines": [_line(1_000_000)]}
    for missing in ("recipient", "note", "lines"):
        await _refused(db, 422, txn, "cash_withdrawal", **{**ok, missing: None})
    await _refused(db, 422, txn, "cash_withdrawal", **{**ok, "lines": [_line(1_000_000, note=" ")]})
    await _refused(
        db, 422, txn, "cash_withdrawal", **{**ok, "lines": [_line(1_000_000, "cash_withdrawal")]}
    )
    assert (await _justify(db, txn, "cash_withdrawal", **ok))["state"] == "justified"


async def test_a_cash_withdrawal_debit_cannot_dodge_its_breakdown_by_another_category(db) -> None:
    txn = await _debit(db, "cash_withdrawal", 1_000_000)
    for category in ("tax", "other", "payroll", "bank_charges"):
        await _refused(db, 422, txn, category, note="anything")
    ok = {"recipient": "CONTOH PENARIK", "note": "cash for the week", "lines": [_line(1_000_000)]}
    assert (await _justify(db, txn, "cash_withdrawal", **ok))["state"] == "justified"


async def test_cash_withdrawal_lines_must_sum_exactly_to_the_debit(db) -> None:
    txn = await _debit(db, "cash_withdrawal", 1_000_000)
    base = {"recipient": "CONTOH PENARIK", "note": "cash"}
    short = await _refused(
        db, 422, txn, "cash_withdrawal", **base, lines=[_line(600_000), _line(399_999)]
    )
    assert "1 IDR missing" in short
    over = await _refused(db, 422, txn, "cash_withdrawal", **base, lines=[_line(1_000_001)])
    assert "1 IDR too much" in over
    assert await db.fetchval("SELECT count(*) FROM outgoing_justifications") == 0
    await _justify(db, txn, "cash_withdrawal", **base, lines=[_line(600_000), _line(400_000)])
    item = (await load_outgoings(db, state="justified"))["items"][0]
    assert [ln["amount_idr"] for ln in item["justification"]["lines"]] == [600_000, 400_000]


async def test_lines_are_refused_for_any_other_category(db) -> None:
    txn = await _debit(db)
    await _refused(db, 422, txn, "tax", lines=[_line(1_000_000)])


async def test_a_paired_fee_cannot_be_justified_directly_and_follows_its_transfer(db) -> None:
    sid = await _statement(db)
    transfer = await _row(db, sid, "transfer_out", 12_000_000, TRANSFER, AT, reference="R1")
    fee = await _row(db, sid, "transfer_fee", 2_500, "x", AT, reference="R1")
    await _refused(db, 409, fee)

    def by_id(result):
        return {i["id"]: i for i in result["items"]}

    before = by_id(await load_outgoings(db, state="all"))
    assert (before[fee]["state"], before[fee]["justification"]) == ("to_justify", None)

    await _justify(db, transfer, "vendor_case", recipient="CONTOH PENERIMA")
    after = await load_outgoings(db, state="all")
    assert (by_id(after)[transfer]["state"], by_id(after)[fee]["state"]) == ("justified",) * 2
    followed = by_id(after)[fee]["justification"]
    assert followed["follows_txn_id"] == transfer and followed["category"] == "vendor_case"
    assert (after["n"], after["n_justified"]) == (0, 2)

    await undo_justification(db, txn_id=transfer, undone_by=WHO)
    assert by_id(await load_outgoings(db, state="all"))[fee]["state"] == "to_justify"


async def test_an_unpaired_fee_can_be_justified_on_its_own(db) -> None:
    fee = await _debit(db, "transfer_fee", 2_500)
    assert (await _justify(db, fee, "bank_charges"))["state"] == "justified"


async def test_undo_the_same_day_but_not_a_justification_of_yesterday(db) -> None:
    txn = await _debit(db)
    with pytest.raises(OutgoingError) as err:
        await undo_justification(db, txn_id=txn, undone_by=WHO)
    assert err.value.status == 409  # nothing to undo
    await _justify(db, txn)
    await undo_justification(db, txn_id=txn, undone_by=WHO)
    row = await db.fetchrow("SELECT undone_by, undone_at FROM outgoing_justifications")
    assert row["undone_by"] == WHO and row["undone_at"] is not None

    await _justify(db, txn)
    await db.execute(
        "UPDATE outgoing_justifications SET justified_at = now() - interval '2 days'"
        " WHERE undone_at IS NULL"
    )
    with pytest.raises(OutgoingError) as err:
        await undo_justification(db, txn_id=txn, undone_by=WHO)
    assert err.value.status == 409
    with pytest.raises(OutgoingError) as err:
        await undo_justification(db, txn_id=999_999, undone_by=WHO)
    assert err.value.status == 404


async def test_the_list_filters_by_state_and_carries_the_justification(db) -> None:
    sid = await _statement(db)
    done = await _row(db, sid, "payroll", 7_000_000, "BULAN OKTOBER", AT)
    todo = await _row(db, sid, "tax", 1_000_000, "x", AT.replace(hour=14))
    await _justify(db, done, "payroll", note="October")
    both = await load_outgoings(db, state="all")
    assert {i["id"]: i["state"] for i in both["items"]} == {done: "justified", todo: "to_justify"}
    # the default is what the Slice 1 view has always listed: only what is still to justify
    assert [i["id"] for i in (await load_outgoings(db))["items"]] == [todo]
    assert [i["id"] for i in (await load_outgoings(db, state="to_justify"))["items"]] == [todo]
    only = (await load_outgoings(db, state="justified"))["items"]
    assert [i["id"] for i in only] == [done]
    assert {k: only[0]["justification"][k] for k in ("category", "note", "by", "lines")} == {
        "category": "payroll",
        "note": "October",
        "by": WHO,
        "lines": [],
    }
    assert (both["n"], both["total_idr"], both["n_justified"]) == (1, 1_000_000, 1)


async def _case(db, client="Contoh Klien", service="Contoh Layanan") -> int:
    cid = await db.fetchval("INSERT INTO clients (full_name) VALUES ($1) RETURNING id", client)
    tid = None
    if service:
        tid = await db.fetchval(
            "INSERT INTO practice_types (code, name) VALUES ($1, $2) RETURNING id",
            uuid.uuid4().hex[:8],
            service,
        )
    return await db.fetchval(
        "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id", cid, tid
    )


async def test_the_list_names_each_case_by_client_and_service(db) -> None:
    kitas = await _case(db, "Contoh Klien", "Contoh Layanan")
    bare = await _case(db, "Klien Kedua", None)
    sid = await _statement(db)
    pnbp = await _row(db, sid, "government_fee", 2_000_000, "x", AT)
    cash = await _row(db, sid, "cash_withdrawal", 1_000_000, "x", AT.replace(hour=14))
    plain = await _row(db, sid, "tax", 500_000, "x", AT.replace(hour=15))
    await _justify(db, pnbp, "pnbp_case", practice_id=kitas)
    await _justify(
        db,
        cash,
        "cash_withdrawal",
        recipient="CONTOH PENARIK",
        note="cash",
        lines=[_line(600_000, practice_id=bare), _line(400_000)],
    )
    await _justify(db, plain, "tax")
    items = {
        i["id"]: i["justification"] for i in (await load_outgoings(db, state="justified"))["items"]
    }
    assert items[pnbp]["practice_id"] == kitas
    assert items[pnbp]["case"] == {"client": "Contoh Klien", "service": "Contoh Layanan"}
    assert items[cash]["case"] is None
    assert [ln["case"] for ln in items[cash]["lines"]] == [
        {"client": "Klien Kedua", "service": None},
        None,
    ]
    assert items[plain]["case"] is None


async def test_the_cash_out_is_never_written(db) -> None:
    count = "SELECT count(*) FROM weekly_cashout"
    before = await db.fetchval(count)
    txn = await _debit(db, "government_fee")
    await _justify(db, txn, "pnbp_case")
    await undo_justification(db, txn_id=txn, undone_by=WHO)
    await _justify(db, txn, "pnbp_case")
    assert await db.fetchval(count) == before


# ── owner 2026-11-22: «Justify all PNBP», and the code debited twice ───────────────────────


async def _pnbp(db, sid, code, amount=1_000_000, hour=10):
    return await _row(
        db, sid, "government_fee", amount, f"EMPN BILLING  {code}  ", AT.replace(hour=hour)
    )


async def _batch_refused(db, status, txn_ids) -> str:
    with pytest.raises(OutgoingError) as err:
        await justify_pnbp_batch(db, txn_ids=txn_ids, justified_by=WHO)
    assert err.value.status == status
    return err.value.detail


async def test_justify_all_pnbp_writes_one_row_per_debit_sharing_one_batch(db) -> None:
    sid = await _statement(db)
    a = await _pnbp(db, sid, "820261121000001", 5_000_000)
    b = await _pnbp(db, sid, "820261121000002", 1_000_000)
    cash_out = await db.fetchval("SELECT count(*) FROM weekly_cashout")
    done = await justify_pnbp_batch(db, txn_ids=[b, a], justified_by=WHO)
    assert (done["n"], done["total_idr"], done["state"]) == (2, 6_000_000, "justified")
    rows = await db.fetch(
        "SELECT txn_id, category, practice_id, note, justified_by, batch_id"
        " FROM outgoing_justifications ORDER BY txn_id"
    )
    assert [r["txn_id"] for r in rows] == sorted([a, b])
    assert {(r["category"], r["practice_id"], r["note"], r["justified_by"]) for r in rows} == {
        ("pnbp_case", None, None, WHO)
    }
    assert {str(r["batch_id"]) for r in rows} == {done["batch_id"]}
    listed = (await load_outgoings(db, state="justified"))["items"]
    assert {i["justification"]["batch_id"] for i in listed} == {done["batch_id"]}
    assert {i["justification"]["batch_active_count"] for i in listed} == {len(listed)}
    assert await db.fetchval("SELECT count(*) FROM weekly_cashout") == cash_out


async def test_one_justified_by_hand_carries_no_batch(db) -> None:
    txn = await _pnbp(db, await _statement(db), "820261121000003")
    await _justify(db, txn, "pnbp_case")
    item = (await load_outgoings(db, state="justified"))["items"][0]
    assert item["justification"]["batch_id"] is None


async def test_the_batch_is_all_or_nothing(db) -> None:
    sid = await _statement(db)
    ok = await _pnbp(db, sid, "820261121000004")
    tax = await _row(db, sid, "tax", 1_000, "x", AT)
    no_code = await _row(db, sid, "government_fee", 1_000, "EMPN BILLING", AT)
    credit = await _row(
        db, sid, "government_fee", 1_000, "EMPN BILLING  820261121000009", AT, direction="credit"
    )
    done = await _pnbp(db, sid, "820261121000005")
    await _justify(db, done, "pnbp_case")
    assert "only PNBP" in await _batch_refused(db, 422, [ok, tax])
    assert "only PNBP" in await _batch_refused(db, 422, [ok, credit])
    assert "e-billing code" in await _batch_refused(db, 422, [ok, no_code])
    assert "already justified" in await _batch_refused(db, 409, [ok, done])
    await _batch_refused(db, 404, [ok, 999_999])
    await _batch_refused(db, 422, [ok, ok])
    await _batch_refused(db, 422, [])
    await _batch_refused(db, 422, list(range(1, BATCH_LIMIT + 2)))
    # nothing of the refused batches was written: only the one justified by hand stands
    assert await db.fetchval("SELECT count(*) FROM outgoing_justifications") == 1


async def test_a_code_debited_twice_is_counted_and_left_to_justify_by_hand(db) -> None:
    sid = await _statement(db)
    first = await _pnbp(db, sid, "820261019000001", hour=11)
    again = await _pnbp(db, sid, "820261019000001", hour=15)
    alone = await _pnbp(db, sid, "820261019000002")
    tax = await _row(db, sid, "tax", 1_000, "x", AT)
    items = {i["id"]: i for i in (await load_outgoings(db))["items"]}
    assert [items[t]["reading"]["code_debits"] for t in (first, again, alone)] == [2, 2, 1]
    assert "code_debits" not in items[tax]["reading"]
    assert "more than one debit" in await _batch_refused(db, 409, [alone, again])
    assert await db.fetchval("SELECT count(*) FROM outgoing_justifications") == 0
    # by hand it is still possible, with what the accountant knows
    await _justify(db, again, "other", note="paid twice, refund asked")
    # the count is the bank's: a justification does not hide the second debit
    items = {i["id"]: i for i in (await load_outgoings(db, state="all"))["items"]}
    assert items[first]["reading"]["code_debits"] == 2


async def test_the_list_and_the_batch_read_a_code_exactly_as_the_reader_shows_it(db) -> None:
    # Gate r1 on #605: a SQL btrim() kept a leading tab that the shown reading strips, so the
    # same code counted 1 and 0 and the batch let a double payment through.
    sid = await _statement(db)
    padded = await _row(db, sid, "government_fee", 1_000, "\tEMPN BILLING  907000001", AT)
    plain = await _row(db, sid, "government_fee", 1_000, "EMPN BILLING\t907000001 ", AT)
    items = {i["id"]: i["reading"] for i in (await load_outgoings(db))["items"]}
    assert [items[t]["billing_code"] for t in (padded, plain)] == ["907000001"] * 2
    assert [items[t]["code_debits"] for t in (padded, plain)] == [2, 2]
    assert "more than one debit" in await _batch_refused(db, 409, [padded])
    assert await db.fetchval("SELECT count(*) FROM outgoing_justifications") == 0


async def test_undo_batch_takes_back_what_is_active_today_and_deletes_nothing(db) -> None:
    sid = await _statement(db)
    a = await _pnbp(db, sid, "820261121000006")
    b = await _pnbp(db, sid, "820261121000007")
    batch = uuid.UUID((await justify_pnbp_batch(db, txn_ids=[a, b], justified_by=WHO))["batch_id"])
    await undo_justification(db, txn_id=a, undone_by=WHO)  # one taken back on its own first
    assert (await undo_batch(db, batch_id=batch, undone_by=WHO))["n"] == 1
    with pytest.raises(OutgoingError) as err:
        await undo_batch(db, batch_id=batch, undone_by=WHO)
    assert err.value.status == 409
    assert (
        await db.fetchval(
            "SELECT count(*) FROM outgoing_justifications WHERE undone_at IS NOT NULL"
        )
        == 2
    )
    assert (await load_outgoings(db))["n"] == 2  # both to justify again

    again = uuid.UUID((await justify_pnbp_batch(db, txn_ids=[a, b], justified_by=WHO))["batch_id"])
    await db.execute(
        "UPDATE outgoing_justifications SET justified_at = now() - interval '2 days'"
        " WHERE batch_id = $1",
        again,
    )
    with pytest.raises(OutgoingError) as err:
        await undo_batch(db, batch_id=again, undone_by=WHO)
    assert err.value.status == 409
    with pytest.raises(OutgoingError) as err:
        await undo_batch(db, batch_id=uuid.uuid4(), undone_by=WHO)
    assert err.value.status == 409
    assert (
        await db.fetchval("SELECT count(*) FROM outgoing_justifications WHERE undone_at IS NULL")
        == 2
    )
