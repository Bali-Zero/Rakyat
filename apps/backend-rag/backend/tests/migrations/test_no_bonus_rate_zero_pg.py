"""Migration 390: a rate of 0 is "no bonus", on the real writer and an isolated schema."""

from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import pytest

from backend.app.routers.hr import delivery_bonus_reviews
from backend.app.services.hr.delivery_bonus import (
    delivery_desk,
    get_delivery_bonus,
    record_delivery_bonus,
    resolve_delivery_bonus,
)
from backend.db.migration_base import split_migration_sql
from backend.tests.migrations.test_delivery_bonus_events_pg import db  # noqa: F401

STAFF = "consultant@example.invalid"
FORWARD, ROLLBACK = split_migration_sql(
    (
        Path(__file__).resolve().parents[2] / "db/migrations_v2/390_no_bonus_rate_zero.sql"
    ).read_text()
)


async def _practice(db, invoices=()):  # noqa: F811
    pid = await db.fetchval(
        """INSERT INTO practices(status, assigned_to, practice_type_id, payment_status)
           VALUES ('completed', $1, 1, 'paid') RETURNING id""",
        STAFF,
    )
    for amount in invoices:
        await db.execute(
            "INSERT INTO invoices(practice_id, amount_idr) VALUES ($1, $2)", pid, amount
        )
    return pid


async def _ledger(db, pid):  # noqa: F811
    return await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", pid)


async def _reviews(db):  # noqa: F811
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = db
    reviews = await delivery_bonus_reviews({"role": "admin", "user_id": "reviewer"}, pool)
    return [i["practice_id"] for i in reviews["items"]]


@pytest.mark.asyncio
async def test_a_custom_service_is_no_bonus_and_never_reaches_payroll_or_review(db):  # noqa: F811
    await db.execute(FORWARD)
    pid = await _practice(db)
    result = await record_delivery_bonus(db, pid, "other_custom", STAFF, "service_delivered")
    assert result["status"] == "no_bonus" and result["amount_idr"] == 0
    assert await _ledger(db, pid) == 0
    # HR confirming the recorded rate books nothing; another amount is refused.
    assert (await resolve_delivery_bonus(db, pid, 0, "hr-reviewer"))["status"] == "no_bonus"
    with pytest.raises(ValueError, match="preserved"):
        await resolve_delivery_bonus(db, pid, 50000, "hr-reviewer")
    assert await _ledger(db, pid) == 0
    assert pid not in await _reviews(db)
    desk = await delivery_desk(db, "tm-1")
    assert {r["practice_id"]: r["status"] for r in desk["recent"]}[pid] == "no_bonus"
    assert desk["totals"]["awaiting_rate"] == 0 and desk["totals"]["payroll_locked"] == 0


@pytest.mark.asyncio
async def test_hr_valuing_an_unpriced_receipt_at_zero_books_nothing(db):  # noqa: F811
    await db.execute(FORWARD)
    pid = await _practice(db)
    waiting = await record_delivery_bonus(db, pid, "unpriced_demo", STAFF, "service_delivered")
    assert waiting["status"] == "awaiting_rate"
    assert pid in await _reviews(db)
    assert (await resolve_delivery_bonus(db, pid, 0, "hr-reviewer"))["status"] == "no_bonus"
    assert await _ledger(db, pid) == 0
    assert pid not in await _reviews(db)


@pytest.mark.asyncio
async def test_a_booked_receipt_always_carries_a_positive_amount(db):  # noqa: F811
    await db.execute(FORWARD)
    pid = await _practice(db)
    await record_delivery_bonus(db, pid, "other_custom", STAFF, "service_delivered")
    with pytest.raises(asyncpg.CheckViolationError) as refused:
        await db.execute(
            "UPDATE hr_delivery_bonus_events SET ledger_id=1 WHERE practice_id=$1", pid
        )
    assert refused.value.constraint_name == "hr_delivery_bonus_events_booked_positive"


@pytest.mark.asyncio
async def test_the_next_case_names_no_bonus_and_a_payment_below_price(db):  # noqa: F811
    await db.execute(FORWARD)
    custom = await db.fetchval(
        "INSERT INTO practice_types(code, name) VALUES ('other_custom', 'Custom') RETURNING id"
    )
    extension = await db.fetchval(
        "INSERT INTO practice_types(code, name) VALUES ('ext_b1_voa', 'Extension B1') RETURNING id"
    )
    await db.execute("UPDATE practices SET practice_type_id=$1 WHERE id=2", custom)
    case = (await delivery_desk(db, "tm-1"))["next_case"]
    assert case["practice_id"] == 2 and case["amount_idr"] == 0 and not case["forfeits"]
    await db.execute("UPDATE practices SET practice_type_id=$1 WHERE id=2", extension)
    await db.execute("INSERT INTO invoices(practice_id, amount_idr) VALUES (2, 800000)")
    case = (await delivery_desk(db, "tm-1"))["next_case"]
    assert case["amount_idr"] == 15000 and case["forfeits"]
    await db.execute("UPDATE invoices SET amount_idr=850000 WHERE practice_id=2")
    assert not (await delivery_desk(db, "tm-1"))["next_case"]["forfeits"]


@pytest.mark.asyncio
async def test_390_turns_unbooked_custom_receipts_into_no_bonus_and_spares_the_rest(db):  # noqa: F811
    # An earlier custom rate edited in the HR editor is replaced, not kept beside it.
    await db.execute(
        """INSERT INTO hr_bonus_rates(practice_type_code, is_active, effective_from, amount_idr)
           VALUES ('other_custom', true, '2020-01-01', 50000)"""
    )
    november, october, resolved, ineligible, other = [await _practice(db) for _ in range(5)]
    await db.execute(
        """INSERT INTO hr_delivery_bonus_events(practice_id, employee_id, service_code, event_kind,
               earned_at, awarded_at, resolved_by, payroll_ineligible)
           SELECT x.p, x.e, x.code, 'service_delivered', x.at, x.at, x.r, x.e IS NULL
           FROM (VALUES ($1::int, 1, 'other_custom', timestamptz '2026-11-25 08:04+00', NULL),
                        ($2, 1, 'other_custom', timestamptz '2026-10-20 08:00+00', NULL),
                        ($3, 1, 'other_custom', timestamptz '2026-11-25 08:00+00', 'hr-reviewer'),
                        ($4, NULL, 'other_custom', timestamptz '2026-11-25 08:00+00', NULL),
                        ($5, 1, 'visa_demo', timestamptz '2026-11-25 08:00+00', NULL)
                ) x(p, e, code, at, r)""",
        november,
        october,
        resolved,
        ineligible,
        other,
    )
    await db.execute(
        "UPDATE hr_delivery_bonus_events SET amount_idr=7 WHERE practice_id=$1", resolved
    )
    await db.execute(FORWARD)
    rate = await db.fetchrow(
        "SELECT id, amount_idr, effective_from::text AS start FROM hr_bonus_rates "
        "WHERE practice_type_code='other_custom' AND is_active"
    )
    assert (rate["amount_idr"], rate["start"]) == (0, "2026-11-01")
    state = {
        r["practice_id"]: (r["amount_idr"], r["bonus_rate_id"])
        for r in await db.fetch(
            "SELECT practice_id, amount_idr, bonus_rate_id FROM hr_delivery_bonus_events"
        )
    }
    assert state == {
        november: (0, rate["id"]),
        october: (None, None),
        resolved: (7, None),
        ineligible: (None, None),
        other: (None, None),
    }
    assert (await get_delivery_bonus(db, november))["status"] == "no_bonus"
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id <> 1") == 0


@pytest.mark.asyncio
async def test_the_rollback_keeps_no_bonus_receipts_and_then_removes_the_rule(db):  # noqa: F811
    await db.execute(FORWARD)
    pid = await _practice(db)
    await record_delivery_bonus(db, pid, "other_custom", STAFF, "service_delivered")
    with pytest.raises(asyncpg.RaiseError, match="Preserve no-bonus receipts"):
        async with db.transaction():
            await db.execute(ROLLBACK)
    await db.execute("DELETE FROM hr_delivery_bonus_events WHERE practice_id=$1", pid)
    await db.execute(ROLLBACK)
    assert not await db.fetchval(
        "SELECT count(*) FROM hr_bonus_rates WHERE practice_type_code='other_custom'"
    )
    assert not await db.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname='hr_delivery_bonus_events_booked_positive'"
    )
