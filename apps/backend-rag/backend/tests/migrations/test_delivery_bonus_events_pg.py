"""Exercise the real issuance trigger and payroll writer on an isolated PostgreSQL schema."""

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.services.hr.delivery_bonus import (
    delivery_desk,
    record_delivery_bonus,
    resolve_delivery_bonus,
)
from backend.db.migration_base import split_migration_sql
from backend.tests.migrations.test_migration_357_hr_bonus_manual import _DDL, _dsn, _parts

MIGRATION = Path(__file__).resolve().parents[2] / "db/migrations_v2/384_delivery_bonus.sql"


@pytest_asyncio.fixture
async def db():
    conn = await asyncpg.connect(_dsn())
    transaction = conn.transaction()
    await transaction.start()
    try:
        schema = "delivery_" + uuid4().hex
        await conn.execute(f"CREATE SCHEMA {schema}; SET LOCAL search_path TO {schema}")
        await conn.execute(_DDL)
        await conn.execute(_parts()[0])
        await conn.execute("""
            ALTER TABLE team_members ADD COLUMN email text, ADD COLUMN role text, ADD COLUMN active boolean DEFAULT true;
            ALTER TABLE hr_employees ADD COLUMN is_active boolean DEFAULT true, ADD COLUMN profile jsonb;
            CREATE TABLE hr_bonus_rates(id serial PRIMARY KEY, practice_type_code text UNIQUE,
                is_active boolean, effective_from date, effective_to date, amount_idr bigint);
            CREATE TABLE hr_payroll_periods(payroll_month int, payroll_year int, status text);
            CREATE TABLE practice_types(id serial PRIMARY KEY, code text, name text);
            ALTER TABLE practices ADD COLUMN assigned_to text, ADD COLUMN practice_type_id int,
                ADD COLUMN start_date timestamptz, ADD COLUMN created_at timestamptz DEFAULT now(), ADD COLUMN payment_status text;
            ALTER TABLE hr_bonus_ledger ADD COLUMN bonus_rate_id int REFERENCES hr_bonus_rates(id);
            UPDATE team_members SET email='consultant@example.invalid',role='consultant';
            INSERT INTO practice_types(code,name) VALUES ('visa_demo','Example new visa');
            INSERT INTO hr_bonus_rates(practice_type_code,is_active,effective_from,amount_idr)
                VALUES ('visa_demo',true,'2020-01-01',125000);
            UPDATE practices SET assigned_to='consultant@example.invalid', practice_type_id=1, payment_status='paid';
            UPDATE practices SET status='on_process' WHERE id=2;
        """)
        await conn.execute(split_migration_sql(MIGRATION.read_text())[0])
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_issuance_pays_once_before_completion_reassignment_cannot_duplicate_it(db):
    issued = await record_delivery_bonus(
        db, 2, "visa_demo", "consultant@example.invalid", "electronic_visa_issued"
    )
    assert issued["amount_idr"] == 125000 and issued["status"] == "pending"
    assert await db.fetchval("SELECT status FROM practices WHERE id=2") == "on_process"
    assert (await delivery_desk(db, "tm-1"))["next_case"]["already_earned"]
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    replay = await record_delivery_bonus(
        db, 2, "visa_demo", "replacement@example.invalid", "service_delivered"
    )
    assert replay == issued
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2") == 1


@pytest.mark.asyncio
async def test_missing_rate_is_retained_and_explicitly_resolved_once(db):
    await db.execute("UPDATE practices SET status='completed' WHERE id=2")
    result = await record_delivery_bonus(
        db, 2, "tax_demo", "consultant@example.invalid", "service_delivered"
    )
    assert result["status"] == "awaiting_rate" and result["amount_idr"] is None
    assert (await delivery_desk(db, "tm-1"))["recent"][0]["practice_id"] == 2
    assert (await resolve_delivery_bonus(db, 2, 250000, "hr-reviewer"))["amount_idr"] == 250000
    await resolve_delivery_bonus(db, 2, 250000, "hr-reviewer")
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=2") == 1


@pytest.mark.asyncio
async def test_legacy_award_survives_upgrade_and_unqualified_insert_is_rejected(db):
    result = await record_delivery_bonus(
        db, 1, "visa_fake", "replacement@example.invalid", "service_delivered"
    )
    assert result["amount_idr"] == 100
    assert await db.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=1") == 1
    with pytest.raises(asyncpg.RaiseError):
        async with db.transaction():
            await db.execute("""INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr)
                            VALUES(2,1,'visa_demo',100)""")
