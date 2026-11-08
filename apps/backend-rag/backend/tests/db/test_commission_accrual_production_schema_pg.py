"""Commission accrual on the PRODUCTION shape of ``practices`` (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Production ``practices`` has no ``total_invoiced_idr`` and no ``completed_at``; the
partner conftest stub does, which is why the stub-based suites never saw the failure.
Synthetic data only. The commission base column is the owner's decision: with a
referral on this schema accrual must keep failing loudly and write nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.crm.partners import events as partner_events
from backend.services.crm.partners.commission_engine import CommissionEngine
from backend.services.events.event_bus import EventBus

pytestmark = pytest.mark.integration

CONSUMER = "event_bus:practice.status_changed"

_SCHEMA_SQL = """
CREATE TABLE practices (
    id             SERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT NOT NULL DEFAULT 'pending',
    payment_status TEXT NOT NULL DEFAULT 'pending',
    client_id      INTEGER
);
CREATE TABLE system_settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
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
    -- NOTE: In production, partner_referrals.practice_id FK references practices(id) INTEGER.
    -- FK omitted: PostgreSQL disallows FK constraints on permanent tables referencing temp tables.
    -- The private practices table uses INTEGER (SERIAL) matching production. Type is enforced here.
    practice_id          INTEGER NOT NULL,
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
    -- practice_id is INTEGER matching production practices.id.
    -- FK omitted: PostgreSQL disallows FK constraints on permanent tables referencing temp tables.
    practice_id              INTEGER,
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
        assert {"total_invoiced_idr", "completed_at"}.isdisjoint(r["column_name"] for r in columns)

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


async def completed_paid_practice(pool: asyncpg.Pool) -> int:
    return await pool.fetchval(
        "INSERT INTO practices (status, payment_status) VALUES ('completed', 'paid') RETURNING id"
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


async def add_referral(pool: asyncpg.Pool, practice_id: int) -> None:
    partner_id = await pool.fetchval(
        "INSERT INTO partners (full_name, email, entity_type) "
        "VALUES ('Synthetic Partner', 'partner@example.test', 'individual') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO partner_referrals (partner_id, practice_id) VALUES ($1, $2)",
        partner_id,
        practice_id,
    )


@pytest.mark.asyncio
async def test_completed_paid_practice_without_referral_returns_none(env: Env) -> None:
    practice_id = await completed_paid_practice(env.pool)

    async with env.pool.acquire() as conn:
        assert await CommissionEngine(conn).accrue_from_practice(practice_id) is None


@pytest.mark.asyncio
async def test_bus_acks_the_outbox_row_when_there_is_no_referral(env: Env) -> None:
    practice_id = await completed_paid_practice(env.pool)
    outbox_id = await outbox_row(env.pool, practice_id)

    await deliver(env, practice_id, outbox_id)

    row = await env.pool.fetchrow(
        "SELECT consumed_at, consumer_id FROM events_outbox WHERE id = $1", outbox_id
    )
    assert row["consumed_at"] is not None
    assert row["consumer_id"] == CONSUMER


@pytest.mark.asyncio
async def test_referral_on_the_production_schema_fails_loudly_and_writes_nothing(
    env: Env,
) -> None:
    practice_id = await completed_paid_practice(env.pool)
    await add_referral(env.pool, practice_id)

    async with env.pool.acquire() as conn:
        with pytest.raises(asyncpg.UndefinedColumnError):
            await CommissionEngine(conn).accrue_from_practice(practice_id)

    outbox_id = await outbox_row(env.pool, practice_id)
    await deliver(env, practice_id, outbox_id)

    assert await env.pool.fetchval("SELECT count(*) FROM partner_commissions") == 0
    row = await env.pool.fetchrow(
        "SELECT consumed_at, consumer_id FROM events_outbox WHERE id = $1", outbox_id
    )
    assert row["consumed_at"] is None
    assert row["consumer_id"] is None
