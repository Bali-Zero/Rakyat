"""Migration 388 and the forfeit rule, on the real issuance writer and an isolated schema."""

from decimal import Decimal
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
from backend.tests.migrations.hr_bonus_fixture import apply_forfeit_migration
from backend.tests.migrations.test_delivery_bonus_events_pg import db  # noqa: F401

STAFF = "consultant@example.invalid"
SEEDED = {
    "ext_b1_voa": (15000, 850000),
    "voa_extension": (15000, 850000),
    "ext_c1_tourism": (20000, 1700000),
    "other_mutation_passport": (10000, None),
    "visa_bridging": (100000, None),
    "visa_b1_voa": (20000, None),
    "tax_efin_application": (100000, None),
    "tax_update_data_coretax": (100000, None),
    "tax_close_pma_company": (1500000, None),
    "tax_npwpd_registration": (500000, None),
    "tax_bpjs_employee": (150000, None),
    "tax_bpjs_insurance": (300000, None),
    "tax_annual_company_zero": (500000, None),
}


async def _practice(db, quoted=None, discount=None, invoices=()):  # noqa: F811
    pid = await db.fetchval(
        """INSERT INTO practices(status, assigned_to, practice_type_id, payment_status,
               quoted_price, discount_amount)
           VALUES ('completed', $1, 1, 'paid', $2, $3) RETURNING id""",
        STAFF,
        quoted,
        discount,
    )
    for amount in invoices:
        await db.execute(
            "INSERT INTO invoices(practice_id, amount_idr) VALUES ($1, $2)", pid, amount
        )
    return pid


async def _earn(db, pid, code="ext_b1_voa"):  # noqa: F811
    return await record_delivery_bonus(db, pid, code, STAFF, "service_delivered")


async def _ledger(db, pid):  # noqa: F811
    return await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", pid)


@pytest.mark.asyncio
async def test_invoice_below_price_forfeits_and_never_reaches_payroll_or_review(db):  # noqa: F811
    pid = await _practice(db, invoices=(800000,))
    result = await _earn(db, pid)
    assert result["status"] == "forfeited" and result["amount_idr"] is None
    assert await _ledger(db, pid) == 0
    assert (await get_delivery_bonus(db, pid))["status"] == "forfeited"
    with pytest.raises(ValueError, match="forfeited"):
        await resolve_delivery_bonus(db, pid, 15000, "hr-reviewer")
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = db
    reviews = await delivery_bonus_reviews({"role": "admin", "user_id": "reviewer"}, pool)
    assert pid not in [i["practice_id"] for i in reviews["items"]]
    desk = await delivery_desk(db, "tm-1")
    assert desk["recent"][0]["status"] == "forfeited"
    assert desk["totals"]["awaiting_rate"] == 0 and desk["totals"]["payroll_locked"] == 0


@pytest.mark.asyncio
async def test_a_zero_rate_is_no_bonus_and_never_reaches_payroll_or_review(db):  # noqa: F811
    await db.execute("UPDATE hr_bonus_rates SET amount_idr=0 WHERE practice_type_code='ext_b1_voa'")
    pid = await _practice(db, invoices=(1000000,))
    result = await _earn(db, pid)
    assert result["status"] == "no_bonus" and result["amount_idr"] == 0
    assert await _ledger(db, pid) == 0
    assert (await get_delivery_bonus(db, pid))["status"] == "no_bonus"
    # HR resolving the receipt at its recorded rate books nothing either.
    assert (await resolve_delivery_bonus(db, pid, 0, "hr-reviewer"))["status"] == "no_bonus"
    assert await _ledger(db, pid) == 0
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = db
    reviews = await delivery_bonus_reviews({"role": "admin", "user_id": "reviewer"}, pool)
    assert pid not in [i["practice_id"] for i in reviews["items"]]
    desk = await delivery_desk(db, "tm-1")
    assert desk["recent"][0]["status"] == "no_bonus"
    assert desk["totals"]["awaiting_rate"] == 0 and desk["totals"]["payroll_locked"] == 0


@pytest.mark.asyncio
async def test_invoice_at_price_pays_and_multi_person_invoice_above_price_pays(db):  # noqa: F811
    exact = await _earn(db, await _practice(db, invoices=(850000,)))
    assert exact["amount_idr"] == 15000 and exact["status"] == "pending"
    group = await _practice(db, invoices=(2550000,))
    assert (await _earn(db, group))["amount_idr"] == 15000
    assert await _ledger(db, group) == 1


@pytest.mark.asyncio
async def test_invoices_are_summed_and_unknown_payment_goes_to_hr(db):  # noqa: F811
    assert (await _earn(db, await _practice(db, invoices=(500000, 350000))))["status"] == "pending"
    quoted = await _practice(db, quoted=2000000, discount=1200000)
    assert (await _earn(db, quoted))["status"] == "forfeited"
    undiscounted = await _practice(db, quoted=2000000, discount=0)
    assert (await _earn(db, undiscounted))["status"] == "pending"
    unknown = await _practice(db)
    review = await _earn(db, unknown)
    assert review["status"] == "awaiting_rate" and review["amount_idr"] is None
    assert not await db.fetchval(
        "SELECT forfeited FROM hr_delivery_bonus_events WHERE practice_id=$1", unknown
    )


@pytest.mark.asyncio
async def test_the_net_invoice_is_not_discounted_twice_and_cents_below_price_forfeit(db):  # noqa: F811
    net = await _practice(db, quoted=1000000, discount=150000, invoices=(850000,))
    assert (await _earn(db, net))["status"] == "pending"
    cents = await _practice(db, quoted=Decimal("850000.00"), discount=Decimal("0.25"))
    assert (await _earn(db, cents))["status"] == "forfeited"


@pytest.mark.asyncio
async def test_a_forfeited_practice_refuses_any_direct_ledger_write(db):  # noqa: F811
    pid = await _practice(db, invoices=(800000,))
    assert (await _earn(db, pid))["status"] == "forfeited"
    employee = await db.fetchval(
        "SELECT employee_id FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
    )
    with pytest.raises(asyncpg.RaiseError, match="forfeited"):
        await db.execute(
            """INSERT INTO hr_bonus_ledger(practice_id, employee_id, practice_type_code,
                   amount_idr, awarded_at, status)
               VALUES ($1, $2, 'ext_b1_voa', 15000, now(), 'pending')""",
            pid,
            employee,
        )


@pytest.mark.asyncio
async def test_codes_without_a_floor_ignore_payment(db):  # noqa: F811
    result = await _earn(db, await _practice(db, invoices=(1,)), "visa_bridging")
    assert result["amount_idr"] == 100000 and result["status"] == "pending"


@pytest.mark.asyncio
async def test_the_thirteen_approved_codes_are_seeded(db):  # noqa: F811
    rows = await db.fetch(
        "SELECT practice_type_code, amount_idr, forfeit_below_idr FROM hr_bonus_rates "
        "WHERE notes='Bonus reform 2026-11-27' AND is_active"
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == SEEDED


@pytest.mark.asyncio
async def test_backfill_prices_unbooked_events_forfeits_below_price_and_spares_the_rest(db):  # noqa: F811
    migration = split_migration_sql(
        (
            Path(__file__).resolve().parents[2] / "db/migrations_v2/388_bonus_rates_forfeit.sql"
        ).read_text()
    )
    await db.execute(migration[1])  # back to the state before 388
    ok = await _practice(db, invoices=(850000,))
    low = await _practice(db, invoices=(100000,))
    unknown = await _practice(db)
    flat = await _practice(db)
    resolved = await _practice(db, invoices=(1,))
    await db.execute(
        """INSERT INTO hr_delivery_bonus_events(practice_id, employee_id, service_code, event_kind,
               earned_at, awarded_at, resolved_by)
           SELECT x.p, 1, x.code, 'service_delivered', now(), now(), x.r
           FROM (VALUES ($1::int,'ext_b1_voa',NULL),($2,'ext_b1_voa',NULL),($3,'ext_b1_voa',NULL),
                        ($4,'visa_bridging',NULL),($5,'ext_b1_voa','hr-reviewer')) x(p, code, r)""",
        ok,
        low,
        unknown,
        flat,
        resolved,
    )
    await db.execute(
        "UPDATE hr_delivery_bonus_events SET amount_idr=7 WHERE practice_id=$1", resolved
    )
    await apply_forfeit_migration(db)
    state = {
        r["practice_id"]: (r["amount_idr"], r["forfeited"])
        for r in await db.fetch(
            "SELECT practice_id, amount_idr, forfeited FROM hr_delivery_bonus_events"
        )
    }
    assert state == {
        ok: (15000, False),
        low: (None, True),
        unknown: (None, False),
        flat: (100000, False),
        resolved: (7, False),
    }
    assert (
        await db.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE practice_id = ANY($1)", [ok, flat]
        )
        == 0
    )
