"""Partner fixtures must not recreate or mutate the canonical public schema."""

import uuid

import asyncpg
import pytest

from .conftest import _DEFAULT_DB_URL, _isolated_partner_connection


async def _public_signature(conn):
    tables = [
        "clients",
        "team_members",
        "practices",
        "system_settings",
        "partners",
        "partner_referrals",
        "partner_commissions",
        "partner_audit_log",
        "partner_email_outbox",
        "events_outbox",
    ]
    return {
        "relations": [
            dict(row)
            for row in await conn.fetch(
                "SELECT oid,relname FROM pg_class WHERE relnamespace='public'::regnamespace "
                "AND relname=ANY($1::text[]) ORDER BY relname",
                tables,
            )
        ],
        "constraints": [
            dict(row)
            for row in await conn.fetch(
                "SELECT c.oid,c.conname,pg_get_constraintdef(c.oid,true) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY($1::text[]) "
                "ORDER BY c.oid",
                tables,
            )
        ],
        "columns": [
            dict(row)
            for row in await conn.fetch(
                "SELECT t.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type, "
                "a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_expr "
                "FROM pg_class t JOIN pg_attribute a ON a.attrelid=t.oid "
                "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY($1::text[]) "
                "AND a.attnum>0 AND NOT a.attisdropped ORDER BY t.relname,a.attnum",
                tables,
            )
        ],
        "client_sequence": await conn.fetchval(
            "SELECT pg_sequence_last_value(pg_get_serial_sequence('public.clients','id')::regclass)"
        ),
        "event_sequence": await conn.fetchval(
            "SELECT pg_sequence_last_value(pg_get_serial_sequence('public.events_outbox','id')::regclass)"
        ),
    }


@pytest.mark.asyncio
async def test_private_fixture_preserves_public_fk_and_sequence():
    observer = await asyncpg.connect(_DEFAULT_DB_URL)
    try:
        before = await _public_signature(observer)
        assert any(
            row["conname"] == "team_members_partner_id_fkey" for row in before["constraints"]
        ), "requires the canonical 220-SQL schema"
        async with _isolated_partner_connection(_DEFAULT_DB_URL) as conn:
            schema = await conn.fetchval("SELECT current_schema()")
            assert schema.startswith("nuz_partner_fixture_")
            assert "public" not in await conn.fetchval("SHOW search_path")
            assert await conn.fetchval(
                "SELECT 'clients'::regclass::oid"
            ) != await observer.fetchval("SELECT 'public.clients'::regclass::oid")
            token = uuid.uuid4().hex[:12]
            await conn.execute(
                "INSERT INTO clients(full_name,email) VALUES($1,$2)",
                "Synthetic Fixture Client",
                f"{token}@test.invalid",
            )
            await conn.execute(
                "INSERT INTO team_members(id,name,email,pin_hash,role) VALUES($1,$2,$1,$3,$4)",
                f"{token}@test.invalid",
                "Synthetic Fixture User",
                "test-pin",
                "team",
            )
            await conn.execute(
                "INSERT INTO partners(full_name,email,entity_type) VALUES($1,$2,$3)",
                "Synthetic Fixture Partner",
                f"{token}@test.invalid",
                "individual",
            )
            assert await _public_signature(observer) == before
        assert await observer.fetchval("SELECT to_regnamespace($1)", schema) is None
        assert await _public_signature(observer) == before
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_two_fixture_connections_do_not_share_rows_or_settings():
    async with _isolated_partner_connection(_DEFAULT_DB_URL) as first:
        async with _isolated_partner_connection(_DEFAULT_DB_URL) as second:
            assert await first.fetchval("SELECT current_schema()") != await second.fetchval(
                "SELECT current_schema()"
            )
            await first.execute(
                "INSERT INTO partners(full_name,email,entity_type) VALUES($1,$2,$3)",
                "Synthetic Fixture Partner",
                "isolated@test.invalid",
                "individual",
            )
            await first.execute(
                "UPDATE system_settings SET value='999' WHERE key='partner_accrual_cooling_off_days'"
            )
            assert await second.fetchval("SELECT count(*) FROM partners") == 0
            assert (
                await second.fetchval(
                    "SELECT value FROM system_settings WHERE key='partner_accrual_cooling_off_days'"
                )
                != "999"
            )
