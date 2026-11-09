"""Commission accrual on the PRODUCTION shape of ``practices`` (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Production ``practices`` has no ``total_invoiced_idr``, no ``completed_at`` and no
``service_type``. Owner decision 2026-11-09: date = ``completion_date``, base =
``COALESCE(actual_price, quoted_price)`` read as IDR (``currency`` is ignored).
Synthetic data only.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers import partners as partners_router
from backend.services.crm.partners import emails as partner_emails
from backend.services.crm.partners import events as partner_events
from backend.services.crm.partners.commission_engine import CommissionEngine
from backend.services.events.event_bus import EventBus

pytestmark = pytest.mark.integration

CONSUMER = "event_bus:practice.status_changed"
COMPLETED_ON = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
RAW_TYPE = "KITAS E33G Synthetic"
ABSENT = {"total_invoiced_idr", "completed_at", "service_type"}

_SCHEMA_SQL = """
CREATE TABLE practice_types (
    id       SERIAL PRIMARY KEY,
    code     TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL,
    category TEXT
);
CREATE TABLE clients (id SERIAL PRIMARY KEY, full_name TEXT, email TEXT);
CREATE TABLE team_members (
    id VARCHAR PRIMARY KEY, name TEXT, email TEXT, pin_hash TEXT, role TEXT, partner_id UUID
);
CREATE TABLE practices (
    id               SERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           TEXT NOT NULL DEFAULT 'pending',
    payment_status   TEXT NOT NULL DEFAULT 'pending',
    quoted_price     NUMERIC(14,2),
    actual_price     NUMERIC(14,2),
    currency         TEXT NOT NULL DEFAULT 'USD',
    completion_date  TIMESTAMPTZ,
    client_id        INTEGER,
    practice_type_id INTEGER
);
CREATE TABLE system_settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO system_settings (key, value) VALUES
    ('partner_clawback_auto_writeoff_idr',   '0'),
    ('partner_accrual_cooling_off_days',     '30'),
    ('partner_withholding_rate_pph21',       '2.5'),
    ('partner_withholding_rate_pph23',       '2.0'),
    ('partner_withholding_no_npwp_surcharge','20');
CREATE TABLE events_outbox (
    id           BIGSERIAL PRIMARY KEY,
    channel      TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at  TIMESTAMPTZ,
    consumer_id  TEXT
);
CREATE TABLE partners (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name                 TEXT NOT NULL,
    work_role                 TEXT,
    company_name              TEXT,
    office_address            TEXT,
    email                     TEXT NOT NULL UNIQUE,
    phone                     TEXT,
    preferred_language        TEXT DEFAULT 'id',
    entity_type               TEXT NOT NULL
        CHECK (entity_type IN ('individual','corporate_pt','corporate_cv','foreign')),
    npwp                      TEXT,
    nik                       TEXT,
    tax_withholding_category  TEXT NOT NULL DEFAULT 'tbd'
        CHECK (tax_withholding_category IN ('pph21','pph23','exempt','tbd')),
    fiscal_address            TEXT,
    bank_name                 TEXT,
    bank_account_holder       TEXT,
    bank_account_number       TEXT,
    ewallet_type              TEXT,
    ewallet_number            TEXT,
    payment_currency          TEXT NOT NULL DEFAULT 'IDR',
    iban                      TEXT,
    payment_notes             TEXT,
    default_commission_type   TEXT NOT NULL DEFAULT 'percentage'
        CHECK (default_commission_type IN ('percentage','flat')),
    default_commission_value  NUMERIC(14,4) NOT NULL DEFAULT 10.0,
    onboarding_status         TEXT NOT NULL DEFAULT 'pending_approval'
        CHECK (onboarding_status IN ('pending_approval','active','inactive')),
    -- CATA-5: assigned_to references team_members(id VARCHAR), not users(id UUID)
    assigned_to               VARCHAR,
    pdp_consent_at            TIMESTAMPTZ,
    pdp_consent_version       TEXT,
    terms_accepted_at         TIMESTAMPTZ,
    terms_version             TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- CATA-5: created_by references team_members(id VARCHAR), not users(id UUID)
    created_by                VARCHAR,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at            TIMESTAMPTZ,
    welcome_email_sent_at     TIMESTAMPTZ
);

CREATE TABLE partner_referrals (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id           UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
    practice_id          INTEGER NOT NULL REFERENCES practices(id) ON DELETE RESTRICT,
    share_percent        NUMERIC(5,2) NOT NULL DEFAULT 100.00
        CHECK (share_percent > 0 AND share_percent <= 100),
    referred_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- CATA-5: referred_by_user_id references team_members(id VARCHAR), not users(id UUID)
    referred_by_user_id  VARCHAR,
    notes                TEXT,
    CONSTRAINT partner_referrals_practice_unique_v1 UNIQUE (practice_id)
);

CREATE TABLE partner_commissions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id               UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
    referral_id              UUID REFERENCES partner_referrals(id) ON DELETE RESTRICT,
    practice_id              INTEGER REFERENCES practices(id) ON DELETE RESTRICT,
    entry_type               TEXT NOT NULL
        CHECK (entry_type IN ('accrual','clawback','manual_adjustment')),
    related_commission_id    UUID REFERENCES partner_commissions(id) ON DELETE RESTRICT,
    base_amount_idr          NUMERIC(16,2) NOT NULL,
    commission_type_snapshot TEXT NOT NULL
        CHECK (commission_type_snapshot IN ('percentage','flat')),
    commission_value_snapshot NUMERIC(14,4) NOT NULL,
    rule_source              TEXT NOT NULL DEFAULT 'partner_default'
        CHECK (rule_source IN ('partner_default','manual_override')),
    -- CATA-5: assigned_to_snapshot references team_members(id VARCHAR), not users(id UUID)
    assigned_to_snapshot     VARCHAR,
    gross_amount_idr         NUMERIC(16,2) NOT NULL,
    withholding_category     TEXT NOT NULL DEFAULT 'tbd'
        CHECK (withholding_category IN ('pph21','pph23','exempt','tbd')),
    withholding_rate         NUMERIC(6,4) NOT NULL DEFAULT 0.0,
    withholding_amount_idr   NUMERIC(16,2) NOT NULL DEFAULT 0.0,
    net_amount_idr           NUMERIC(16,2) NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'accrued'
        CHECK (status IN (
            'accrued','approved','paid',
            'clawback_pending','offset_applied',
            'waived','repaid'
        )),
    accrued_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    eligible_for_approval_at TIMESTAMPTZ NOT NULL,
    approved_at              TIMESTAMPTZ,
    -- CATA-5: approved_by references team_members(id VARCHAR), not users(id UUID)
    approved_by              VARCHAR,
    paid_at                  TIMESTAMPTZ,
    -- CATA-5: paid_by references team_members(id VARCHAR), not users(id UUID)
    paid_by                  VARCHAR,
    paid_via                 TEXT,
    payment_reference        TEXT,
    payment_proof_url        TEXT,
    receipt_type             TEXT
        CHECK (receipt_type IS NULL OR receipt_type IN ('kwitansi','invoice','none')),
    receipt_file_url         TEXT,
    manual_override_reason   TEXT,
    clawback_reason          TEXT,
    waiver_reason            TEXT,
    idempotency_key          TEXT UNIQUE,
    commission_email_sent_at TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE partner_email_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_type TEXT NOT NULL CHECK (email_type IN ('welcome','commission_earned')),
    partner_id UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
    commission_id UUID REFERENCES partner_commissions(id) ON DELETE RESTRICT,
    to_email TEXT NOT NULL,
    cc_emails TEXT[] DEFAULT '{}',
    subject TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed_dlq')),
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- CATA-5: created_by references team_members(id VARCHAR), not users(id UUID)
    created_by VARCHAR,
    idempotency_key TEXT UNIQUE
);
"""


@dataclass(frozen=True)
class Env:
    pool: asyncpg.Pool
    bus: EventBus


@pytest_asyncio.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    pool = None
    schema = "commission_prod_" + uuid4().hex
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
            columns = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'practices'"
            )
        assert ABSENT.isdisjoint(r["column_name"] for r in columns)

        async def get_pool() -> asyncpg.Pool:
            return pool

        monkeypatch.setattr(partner_events, "get_pool", get_pool)
        bus = EventBus(db_dsn="", db_pool=pool)
        partner_events.register_partner_handlers(bus)
        yield Env(pool=pool, bus=bus)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def practice(
    pool: asyncpg.Pool,
    *,
    status: str = "completed",
    payment: str = "paid",
    actual: str | None = None,
    quoted: str | None = None,
    completed_on: datetime | None = COMPLETED_ON,
) -> int:
    return await pool.fetchval(
        "INSERT INTO practices (status, payment_status, actual_price, quoted_price, "
        "completion_date) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        status,
        payment,
        Decimal(actual) if actual is not None else None,
        Decimal(quoted) if quoted is not None else None,
        completed_on,
    )


async def partner(pool: asyncpg.Pool, *, kind: str = "percentage", value: str = "10") -> UUID:
    return await pool.fetchval(
        "INSERT INTO partners (full_name, email, entity_type, tax_withholding_category, "
        "default_commission_type, default_commission_value) "
        "VALUES ('Synthetic Partner', $1, 'individual', 'exempt', $2, $3) RETURNING id",
        f"partner-{uuid4().hex[:8]}@example.test",
        kind,
        Decimal(value),
    )


async def refer(pool: asyncpg.Pool, partner_id: UUID, practice_id: int) -> None:
    await pool.execute(
        "INSERT INTO partner_referrals (partner_id, practice_id) VALUES ($1, $2)",
        partner_id,
        practice_id,
    )


async def outbox_row(pool: asyncpg.Pool, practice_id: int) -> int:
    payload = {"practice_id": practice_id, "new_status": "completed"}
    return await pool.fetchval(
        "INSERT INTO events_outbox (channel, payload) VALUES ('practice_changed', $1::text::jsonb) "
        "RETURNING id",
        json.dumps(payload),
    )


async def deliver(env: Env, practice_id: int, outbox_id: int) -> None:
    payload = {"practice_id": practice_id, "new_status": "completed", "_outbox_id": outbox_id}
    await env.bus._handle_pg_event("practice_changed", json.dumps(payload))


async def acked(pool: asyncpg.Pool, outbox_id: int) -> tuple[bool, str | None]:
    row = await pool.fetchrow(
        "SELECT consumed_at, consumer_id FROM events_outbox WHERE id = $1", outbox_id
    )
    return row["consumed_at"] is not None, row["consumer_id"]


def warned_about(caplog, practice_id: int) -> bool:
    return any(
        r.levelno == logging.WARNING and str(practice_id) in r.getMessage() for r in caplog.records
    )


async def commissions(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM partner_commissions")


@pytest.mark.asyncio
async def test_completed_paid_practice_without_referral_returns_none(env: Env) -> None:
    practice_id = await practice(env.pool, actual="1000000")

    async with env.pool.acquire() as conn:
        assert await CommissionEngine(conn).accrue_from_practice(practice_id) is None


@pytest.mark.asyncio
async def test_bus_acks_the_outbox_row_when_there_is_no_referral(env: Env) -> None:
    practice_id = await practice(env.pool, actual="1000000")
    outbox_id = await outbox_row(env.pool, practice_id)

    await deliver(env, practice_id, outbox_id)

    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual", "quoted", "base"),
    [
        ("10000000", "8000000", "10000000"),
        (None, "8000000", "8000000"),
        ("10000000", None, "10000000"),
    ],
    ids=["actual-wins-over-quoted", "quoted-when-no-actual", "actual-only"],
)
async def test_percentage_commission_uses_actual_then_quoted_price(
    env: Env, actual: str | None, quoted: str | None, base: str
) -> None:
    practice_id = await practice(env.pool, actual=actual, quoted=quoted)
    await refer(env.pool, await partner(env.pool), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    await deliver(env, practice_id, outbox_id)

    rows = await commissions(env.pool)
    assert len(rows) == 1
    assert rows[0]["base_amount_idr"] == Decimal(base)
    assert rows[0]["gross_amount_idr"] == Decimal(base) / 10
    assert rows[0]["idempotency_key"] == f"accrual:{practice_id}:{COMPLETED_ON.isoformat()}"
    assert rows[0]["eligible_for_approval_at"] == datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_flat_commission_ignores_the_base(env: Env) -> None:
    practice_id = await practice(env.pool, actual="10000000")
    await refer(env.pool, await partner(env.pool, kind="flat", value="500000"), practice_id)

    async with env.pool.acquire() as conn:
        assert await CommissionEngine(conn).accrue_from_practice(practice_id) is not None

    rows = await commissions(env.pool)
    assert len(rows) == 1
    assert rows[0]["gross_amount_idr"] == Decimal("500000")
    assert rows[0]["base_amount_idr"] == Decimal("10000000")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual", "quoted", "completed_on"),
    [(None, None, COMPLETED_ON), ("10000000", None, None)],
    ids=["no-base", "no-date"],
)
async def test_referral_without_base_or_date_writes_nothing_warns_and_acks(
    env: Env, caplog, actual: str | None, quoted: str | None, completed_on: datetime | None
) -> None:
    practice_id = await practice(env.pool, actual=actual, quoted=quoted, completed_on=completed_on)
    await refer(env.pool, await partner(env.pool), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    with caplog.at_level(logging.WARNING):
        await deliver(env, practice_id, outbox_id)

    assert await commissions(env.pool) == []
    assert warned_about(caplog, practice_id)
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_second_delivery_of_the_same_event_keeps_one_row(env: Env) -> None:
    practice_id = await practice(env.pool, actual="10000000")
    await refer(env.pool, await partner(env.pool), practice_id)

    outbox_ids = [await outbox_row(env.pool, practice_id) for _ in range(2)]
    for outbox_id in outbox_ids:
        await deliver(env, practice_id, outbox_id)

    assert len(await commissions(env.pool)) == 1
    for outbox_id in outbox_ids:
        assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_flat_commission_with_no_prices_is_written_with_a_zero_base(env: Env) -> None:
    practice_id = await practice(env.pool)
    await refer(env.pool, await partner(env.pool, kind="flat", value="500000"), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    await deliver(env, practice_id, outbox_id)

    rows = await commissions(env.pool)
    assert len(rows) == 1
    assert rows[0]["base_amount_idr"] == Decimal("0")
    assert rows[0]["gross_amount_idr"] == Decimal("500000")
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_flat_commission_without_a_completion_date_writes_nothing(env: Env, caplog) -> None:
    practice_id = await practice(env.pool, completed_on=None)
    await refer(env.pool, await partner(env.pool, kind="flat", value="500000"), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    with caplog.at_level(logging.WARNING):
        await deliver(env, practice_id, outbox_id)

    assert await commissions(env.pool) == []
    assert warned_about(caplog, practice_id)
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_percentage_on_a_zero_actual_price_does_not_fall_back_or_write_zero(
    env: Env, caplog
) -> None:
    practice_id = await practice(env.pool, actual="0", quoted="8000000")
    await refer(env.pool, await partner(env.pool), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    with caplog.at_level(logging.WARNING):
        await deliver(env, practice_id, outbox_id)

    assert await commissions(env.pool) == []
    assert warned_about(caplog, practice_id)
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_flat_commission_of_zero_writes_nothing(env: Env, caplog) -> None:
    practice_id = await practice(env.pool, actual="10000000")
    await refer(env.pool, await partner(env.pool, kind="flat", value="0"), practice_id)

    with caplog.at_level(logging.WARNING):
        async with env.pool.acquire() as conn:
            assert await CommissionEngine(conn).accrue_from_practice(practice_id) is None

    assert await commissions(env.pool) == []
    assert warned_about(caplog, practice_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "value", "actual"),
    [("percentage", "10", "0.01"), ("flat", "0.0001", "10000000")],
    ids=["percentage-gross-0.001", "flat-0.0001"],
)
async def test_commission_that_stores_as_zero_is_not_written(
    env: Env, caplog, kind: str, value: str, actual: str
) -> None:
    practice_id = await practice(env.pool, actual=actual)
    await refer(env.pool, await partner(env.pool, kind=kind, value=value), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    with caplog.at_level(logging.WARNING):
        await deliver(env, practice_id, outbox_id)

    assert await commissions(env.pool) == []
    assert warned_about(caplog, practice_id)
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


@pytest.mark.asyncio
async def test_gross_that_rounds_to_one_cent_is_written_as_postgres_stores_it(env: Env) -> None:
    practice_id = await practice(env.pool, actual="0.05")
    await refer(env.pool, await partner(env.pool), practice_id)

    async with env.pool.acquire() as conn:
        assert await CommissionEngine(conn).accrue_from_practice(practice_id) is not None

    rows = await commissions(env.pool)
    assert len(rows) == 1
    assert str(rows[0]["gross_amount_idr"]) == "0.01"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payment"),
    [("on_process", "paid"), ("completed", "pending")],
    ids=["not-completed", "not-paid"],
)
async def test_ineligible_practice_with_referral_writes_nothing(
    env: Env, status: str, payment: str
) -> None:
    practice_id = await practice(env.pool, status=status, payment=payment, actual="10000000")
    await refer(env.pool, await partner(env.pool), practice_id)
    outbox_id = await outbox_row(env.pool, practice_id)

    await deliver(env, practice_id, outbox_id)

    assert await commissions(env.pool) == []
    assert await acked(env.pool, outbox_id) == (True, CONSUMER)


async def paid_commission_for(env: Env) -> tuple[UUID, UUID, int]:
    kitas = await env.pool.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('synthetic_type', 'KITAS E33G Synthetic') "
        "RETURNING id"
    )
    client = await env.pool.fetchval(
        "INSERT INTO clients (full_name, email) VALUES ('Mario Rossi', 'c@example.test') "
        "RETURNING id"
    )
    practice_id = await practice(env.pool, actual="10000000")
    await env.pool.execute(
        "UPDATE practices SET client_id = $1, practice_type_id = $2 WHERE id = $3",
        client,
        kitas,
        practice_id,
    )
    partner_id = await partner(env.pool)
    await refer(env.pool, partner_id, practice_id)
    async with env.pool.acquire() as conn:
        commission_id = await CommissionEngine(conn).accrue_from_practice(practice_id)
    await env.pool.execute(
        "UPDATE partner_commissions SET status = 'paid', paid_at = now(), paid_via = 'bank' "
        "WHERE id = $1",
        commission_id,
    )
    return partner_id, commission_id, practice_id


@pytest.mark.asyncio
async def test_enqueue_commission_earned_sends_the_category_not_the_type_name(env: Env) -> None:
    _, commission_id, _ = await paid_commission_for(env)

    async with env.pool.acquire() as conn:
        await partner_emails.enqueue_commission_earned(conn, commission_id)

    row = await env.pool.fetchrow(
        "SELECT subject, body_markdown FROM partner_email_outbox WHERE commission_id = $1",
        commission_id,
    )
    assert "Visa / KITAS service" in row["body_markdown"]
    assert RAW_TYPE not in row["subject"] + row["body_markdown"]


@pytest.mark.asyncio
async def test_send_commission_earned_sends_the_category_not_the_type_name(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, commission_id, _ = await paid_commission_for(env)
    sent: list[str] = []

    async def fake_post(*, to: str, cc, subject: str, body: str) -> None:
        sent.append(subject + body)

    monkeypatch.setattr(partner_emails, "_post_email", fake_post)
    async with env.pool.acquire() as conn:
        await partner_emails.send_commission_earned(conn, commission_id)

    assert len(sent) == 1
    assert "Visa / KITAS service" in sent[0]
    assert RAW_TYPE not in sent[0]


@pytest.mark.asyncio
async def test_me_referrals_query_runs_on_the_production_shape(env: Env) -> None:
    partner_id, _, _ = await paid_commission_for(env)
    await env.pool.execute(
        "INSERT INTO team_members (id, name, email, role, partner_id) "
        "VALUES ('tm-synthetic', 'Synthetic', 'tm@example.test', 'partner', $1)",
        partner_id,
    )

    rows = await partners_router.me_referrals(
        user={"role": "partner", "user_id": "tm-synthetic"}, pool=env.pool
    )

    assert len(rows) == 1
    assert rows[0]["client_display"] == "Mario R."
    assert rows[0]["service_type"] == "Visa / KITAS service"
    assert rows[0]["process_status"] == "completed"
