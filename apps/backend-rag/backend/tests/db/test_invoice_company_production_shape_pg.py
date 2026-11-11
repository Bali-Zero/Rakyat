"""Invoice-to-company on the PRODUCTION shape of the tables (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
The practice is created by the real ``create_practice`` route, so every column the
route does not name takes its production default (the 2026-11-11 incident was a
fail-closed on a default column value that hand-built rows hid). Synthetic data only.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from pypdf import PdfReader

from backend.app.routers import crm_practices
from backend.app.routers.crm_practices import PracticeCreate, PracticeUpdate
from backend.db.migration_base import split_migration_sql
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.services.invoicing.invoice_service import (
    InvoiceAutomationService,
    InvoiceIssuerDecision,
)

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "db/migrations_v2/356_practices_invoice_company.sql"
)
USER = {"email": "synthetic.staff@example.invalid", "role": "admin"}

_SCHEMA_SQL = """
CREATE TABLE practice_types (
    id SERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category TEXT,
    base_price NUMERIC, is_active BOOLEAN NOT NULL DEFAULT true, required_documents JSONB
);
CREATE TABLE clients (
    id SERIAL PRIMARY KEY, full_name VARCHAR NOT NULL, email VARCHAR, phone VARCHAR,
    address TEXT, assigned_to VARCHAR, last_interaction_date TIMESTAMPTZ,
    google_drive_folder_id VARCHAR, nationality VARCHAR,
    created_by VARCHAR, deleted_at TIMESTAMPTZ
);
CREATE TABLE companies (
    id SERIAL PRIMARY KEY, uuid VARCHAR DEFAULT (gen_random_uuid())::text UNIQUE,
    company_name VARCHAR NOT NULL, company_type VARCHAR NOT NULL DEFAULT 'PT PMA',
    nib VARCHAR UNIQUE, npwp_company VARCHAR UNIQUE, registered_address TEXT,
    status VARCHAR DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(), created_by VARCHAR, updated_by VARCHAR
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
    role VARCHAR NOT NULL DEFAULT 'shareholder', is_primary BOOLEAN DEFAULT false,
    status VARCHAR DEFAULT 'active', end_date DATE, ownership_percentage NUMERIC,
    shares_count INTEGER, start_date DATE,
    created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (client_id, company_id)
);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY, uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    client_id INTEGER, practice_type_code VARCHAR, title VARCHAR, description TEXT,
    status VARCHAR DEFAULT 'inquiry', priority VARCHAR DEFAULT 'normal',
    quoted_price NUMERIC, final_price NUMERIC, currency VARCHAR DEFAULT 'USD',
    assigned_to VARCHAR, start_date DATE, expected_completion_date DATE,
    actual_completion_date DATE, notes TEXT, metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, created_by VARCHAR,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, updated_by VARCHAR,
    practice_type_id INTEGER, expiry_date DATE, next_renewal_date DATE,
    inquiry_date TIMESTAMPTZ DEFAULT now(), completion_date TIMESTAMPTZ,
    actual_price NUMERIC, payment_status VARCHAR DEFAULT 'unpaid',
    paid_amount NUMERIC DEFAULT 0, documents JSONB DEFAULT '[]'::jsonb,
    missing_documents JSONB DEFAULT '[]'::jsonb, custom_fields JSONB DEFAULT '{}'::jsonb,
    internal_notes TEXT, user_profile_id UUID, client_visible BOOLEAN DEFAULT true,
    client_summary TEXT, client_notification_sent BOOLEAN DEFAULT false,
    family_member_id BIGINT, discount_amount NUMERIC DEFAULT NULL::numeric,
    discount_reason TEXT, source_idempotency_key TEXT
);
CREATE TABLE activity_log (
    id SERIAL PRIMARY KEY, entity_type TEXT, entity_id INTEGER, action TEXT,
    performed_by TEXT, description TEXT, changes JSONB, performed_at TIMESTAMPTZ DEFAULT now()
);
"""


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    schema = "invoice_company_" + uuid4().hex
    created = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        async with created.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
            up, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
            await conn.execute(up.replace("public", schema))
            await conn.execute(
                "INSERT INTO practice_types (code, name, category, base_price) VALUES"
                " ('tax_annual_company','Tax annual','tax_standalone',1000000),"
                " ('pt_pma_setup','PT setup','corporate',2000000),"
                " ('kitas_working_onshore','KITAS','kitas',3000000)"
            )

        async def no_cache(*_a: object, **_k: object) -> int:
            return 0

        monkeypatch.setattr(crm_practices, "invalidate_cache", no_cache)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def client(
    pool: asyncpg.Pool, name: str = "Synthetic Person", assigned_to: str | None = None
) -> int:
    return await pool.fetchval(
        "INSERT INTO clients (full_name, email, address, assigned_to)"
        " VALUES ($1, 'synthetic.person@example.invalid', 'Synthetic Home Street 1', $2) RETURNING id",
        name,
        assigned_to,
    )


async def company(
    pool: asyncpg.Pool, name: str, npwp: str | None = None, address: str | None = None
) -> int:
    return await pool.fetchval(
        "INSERT INTO companies (company_name, npwp_company, registered_address) VALUES ($1,$2,$3) RETURNING id",
        name,
        npwp,
        address,
    )


async def link(pool: asyncpg.Pool, client_id: int, company_id: int, role: str = "director") -> None:
    await pool.execute(
        "INSERT INTO client_company_links (client_id, company_id, role) VALUES ($1,$2,$3)",
        client_id,
        company_id,
        role,
    )


async def create(
    pool: asyncpg.Pool, client_id: int, code: str = "tax_annual_company", user: dict = USER, **extra
):
    body = PracticeCreate(client_id=client_id, practice_type_code=code, status="inquiry", **extra)
    return await crm_practices.create_practice(
        request=None,
        practice=body,
        background_tasks=BackgroundTasks(),
        db_pool=pool,
        current_user=user,
    )


async def pdf_text(pool: asyncpg.Pool, practice_id: int) -> str:
    """Run the real ``trigger_on_sending_invoice``; only delivery and persistence are doubled."""
    now = datetime.now(tz=timezone.utc)
    drive = MagicMock()
    drive.upload_file_to_folder = AsyncMock(return_value={"id": "drive-id", "webViewLink": "x"})
    with (
        patch(
            "backend.services.invoicing.invoice_service.ServiceAccountDriveService",
            return_value=drive,
        ),
        patch(
            "backend.services.invoicing.invoice_service.log_email_attempt",
            AsyncMock(return_value=1),
        ),
        patch("backend.services.invoicing.invoice_service.record_email_result", AsyncMock()),
    ):
        service = InvoiceAutomationService(pool)
    service._resolve_issuer = AsyncMock(
        return_value=InvoiceIssuerDecision(
            entity=ENTITIES[DEFAULT_ENTITY_KEY], routed_at=now, lookup_at=now, source="new"
        )
    )
    sent = AsyncMock(return_value=True)
    service._send_invoice_email_to_client = sent
    service._send_accounting_notification = AsyncMock()
    service._update_practice_with_invoice = AsyncMock()
    with (
        patch(
            "backend.services.invoicing.invoice_service.log_email_attempt",
            AsyncMock(return_value=1),
        ),
        patch("backend.services.invoicing.invoice_service.record_email_result", AsyncMock()),
    ):
        result = await service.trigger_on_sending_invoice(practice_id, USER["email"])
    assert result["success"] is True, result
    pdf = sent.await_args.kwargs["pdf_bytes"]
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def shows(text: str, needle: str) -> bool:
    """The v4 design prints names in capitals: compare case-insensitively."""
    return needle.upper() in text.upper()


@pytest.mark.asyncio
async def test_production_defaults_practice_bills_the_chosen_company(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    co = await company(pool, "PT Synthetic Maju", "01.234.567.8-901.000", "Synthetic Office Road 9")
    created = await create(pool, person, invoice_company_id=co)
    assert created.invoice_company_id == co
    assert await pool.fetchval(
        "SELECT 1 FROM client_company_links WHERE client_id=$1 AND company_id=$2", person, co
    )
    text = await pdf_text(pool, created.id)
    assert shows(text, "PT Synthetic Maju")
    assert shows(text, "NPWP: 01.234.567.8-901.000")
    assert shows(text, "Synthetic Office Road 9")
    assert shows(text, "Attn: Synthetic Person")
    assert not shows(text, "Synthetic Home Street 1")


@pytest.mark.asyncio
async def test_practice_without_company_still_bills_the_individual(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    created = await create(pool, person)
    assert created.invoice_company_id is None
    text = await pdf_text(pool, created.id)
    assert shows(text, "Synthetic Person")
    assert shows(text, "Synthetic Home Street 1")
    assert not shows(text, "Attn:")


@pytest.mark.asyncio
async def test_typed_name_creates_company_and_link_then_reuses_it(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    first = await create(pool, person, invoice_company_name="PT. Synthetic Baru")
    row = await pool.fetchrow(
        "SELECT company_name, company_type, created_by FROM companies WHERE id=$1",
        first.invoice_company_id,
    )
    assert (row["company_name"], row["company_type"]) == ("PT. Synthetic Baru", "PT")
    assert row["created_by"] == USER["email"]
    role = await pool.fetchval(
        "SELECT role FROM client_company_links WHERE client_id=$1 AND company_id=$2",
        person,
        first.invoice_company_id,
    )
    assert role == "Director"
    again = await create(pool, person, invoice_company_name="  pt synthetic   BARU ")
    assert again.invoice_company_id == first.invoice_company_id
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1
    assert await pool.fetchval("SELECT count(*) FROM client_company_links") == 1


@pytest.mark.asyncio
async def test_same_npwp_with_other_name_reuses_and_links_second_person(pool: asyncpg.Pool) -> None:
    first_person, second_person = (
        await client(pool, "Synthetic One"),
        await client(pool, "Synthetic Two"),
    )
    co = await company(pool, "PT Synthetic Lama", "01.234.567.8-901.000")
    created = await create(
        pool,
        second_person,
        invoice_company_name="Totally Other Name",
        invoice_company_npwp="012345678901000",
    )
    assert created.invoice_company_id == co
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM client_company_links WHERE company_id=$1 AND client_id=$2",
            co,
            second_person,
        )
        == 1
    )
    assert first_person != second_person


@pytest.mark.asyncio
async def test_two_name_matches_is_a_409_with_candidates_and_writes_nothing(
    pool: asyncpg.Pool,
) -> None:
    person = await client(pool)
    a = await company(pool, "PT Synthetic Kembar")
    b = await company(pool, "CV. Synthetic  Kembar")
    with pytest.raises(HTTPException) as caught:
        await create(pool, person, invoice_company_name="Synthetic Kembar")
    assert caught.value.status_code == 409
    assert {c["id"] for c in caught.value.detail["candidates"]} == {a, b}
    assert await pool.fetchval("SELECT count(*) FROM practices") == 0
    assert await pool.fetchval("SELECT count(*) FROM client_company_links") == 0


@pytest.mark.asyncio
async def test_update_sets_changes_and_clears_the_company(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    co = await company(pool, "PT Synthetic Update")
    created = await create(pool, person)

    async def patch(**fields):
        return await crm_practices.update_practice(
            request=None,
            practice_id=created.id,
            updates=PracticeUpdate(**fields),
            db_pool=pool,
            current_user=USER,
        )

    out = await patch(invoice_company_id=co)
    assert out["invoice_company_id"] == co and out["invoice_company_name"] == "PT Synthetic Update"
    out = await patch(invoice_company_name="PT Synthetic Typed Later")
    assert out["invoice_company_name"] == "PT Synthetic Typed Later"
    out = await patch(invoice_company_id=None)
    assert out["invoice_company_id"] is None
    out = await patch(priority="high")
    assert out["invoice_company_id"] is None


@pytest.mark.asyncio
async def test_deleting_the_company_falls_back_to_the_individual(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    co = await company(pool, "PT Synthetic Hilang")
    created = await create(pool, person, invoice_company_id=co)
    await pool.execute("DELETE FROM client_company_links WHERE company_id=$1", co)
    await pool.execute("DELETE FROM companies WHERE id=$1", co)
    assert (
        await pool.fetchval("SELECT invoice_company_id FROM practices WHERE id=$1", created.id)
        is None
    )
    assert shows(await pdf_text(pool, created.id), "Synthetic Home Street 1")


@pytest.mark.asyncio
async def test_unknown_company_id_is_a_404(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    with pytest.raises(HTTPException) as caught:
        await create(pool, person, invoice_company_id=999999)
    assert caught.value.status_code == 404
    assert await pool.fetchval("SELECT count(*) FROM practices") == 0


@pytest.mark.asyncio
async def test_suggestion_endpoint_follows_the_autofill_rule(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    only = await company(pool, "PT Synthetic Tunggal")
    await link(pool, person, only, "komisaris")

    async def suggest(code: str | None) -> dict:
        return await crm_practices.invoice_company_suggestion(
            request=None, client_id=person, practice_type_code=code, db_pool=pool, current_user=USER
        )

    assert (await suggest("tax_annual_company"))["suggested_company_id"] == only
    assert (await suggest("pt_pma_setup"))["suggested_company_id"] == only
    visa = await suggest("kitas_working_onshore")
    assert visa["autofill"] is False and visa["suggested_company_id"] is None
    assert [c["id"] for c in visa["companies"]] == [only]
    second = await company(pool, "PT Synthetic Kedua")
    await link(pool, person, second, "Direktur Utama")
    two = await suggest("tax_annual_company")
    assert two["suggested_company_id"] == second
    assert len(two["companies"]) == 2


STAFF = {"email": "synthetic.owner@example.invalid", "role": "staff"}
OTHER = {"email": "synthetic.other@example.invalid", "role": "staff"}


async def counts(pool: asyncpg.Pool) -> tuple[int, int, int]:
    return (
        await pool.fetchval("SELECT count(*) FROM companies"),
        await pool.fetchval("SELECT count(*) FROM client_company_links"),
        await pool.fetchval("SELECT count(*) FROM practices"),
    )


@pytest.mark.asyncio
async def test_company_and_link_writes_need_write_access_to_the_client(pool: asyncpg.Pool) -> None:
    person = await client(pool, assigned_to=STAFF["email"])
    with pytest.raises(HTTPException) as caught:
        await create(pool, person, user=OTHER, invoice_company_name="PT Synthetic Terlarang")
    assert caught.value.status_code == 403
    assert await counts(pool) == (0, 0, 0)
    allowed = await create(pool, person, user=STAFF, invoice_company_name="PT Synthetic Diizinkan")
    assert allowed.invoice_company_id is not None
    plain = await create(pool, person, user=OTHER)
    assert plain.invoice_company_id is None


@pytest.mark.asyncio
async def test_npwp_only_patch_is_rejected_and_never_clears_the_company(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    co = await company(pool, "PT Synthetic Tetap")
    created = await create(pool, person, invoice_company_id=co)
    for fields in ({"invoice_company_npwp": "012345678901000"}, {"invoice_company_name": "  "}):
        with pytest.raises(HTTPException) as caught:
            await crm_practices.update_practice(
                request=None,
                practice_id=created.id,
                updates=PracticeUpdate(**fields),
                db_pool=pool,
                current_user=USER,
            )
        assert caught.value.status_code == 422
    assert (
        await pool.fetchval("SELECT invoice_company_id FROM practices WHERE id=$1", created.id)
        == co
    )


@pytest.mark.asyncio
async def test_a_failing_patch_rolls_back_the_company_and_the_link(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    created = await create(pool, person)
    before = await counts(pool)
    await pool.execute("DROP TABLE activity_log")
    with pytest.raises(HTTPException):
        await crm_practices.update_practice(
            request=None,
            practice_id=created.id,
            updates=PracticeUpdate(invoice_company_name="PT Synthetic Batal", priority="high"),
            db_pool=pool,
            current_user=USER,
        )
    assert await counts(pool) == before
    assert (
        await pool.fetchval("SELECT invoice_company_id FROM practices WHERE id=$1", created.id)
        is None
    )


@pytest.mark.asyncio
async def test_typed_npwp_that_exists_nowhere_never_reuses_a_company_with_another_npwp(
    pool: asyncpg.Pool,
) -> None:
    person = await client(pool)
    other = await company(pool, "PT Synthetic Sama", "99.999.999.9-999.000")
    created = await create(
        pool,
        person,
        invoice_company_name="PT Synthetic Sama",
        invoice_company_npwp="01.234.567.8-901.000",
    )
    assert created.invoice_company_id != other
    assert await pool.fetchval("SELECT count(*) FROM companies") == 2


@pytest.mark.asyncio
async def test_typed_npwp_that_exists_nowhere_reuses_a_same_name_company_without_npwp(
    pool: asyncpg.Pool,
) -> None:
    person = await client(pool)
    bare = await company(pool, "PT Synthetic Polos")
    created = await create(
        pool,
        person,
        invoice_company_name="pt synthetic polos",
        invoice_company_npwp="01.234.567.8-901.000",
    )
    assert created.invoice_company_id == bare
    assert (
        await pool.fetchval("SELECT npwp_company FROM companies WHERE id=$1", bare)
        == "01.234.567.8-901.000"
    )
    assert shows(await pdf_text(pool, created.id), "NPWP: 01.234.567.8-901.000")
    again = await create(
        pool, person, invoice_company_name="Other Name", invoice_company_npwp="012345678901000"
    )
    assert again.invoice_company_id == bare


@pytest.mark.asyncio
async def test_one_npwp_on_two_companies_is_a_409_not_a_first_pick(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    a = await company(pool, "PT Synthetic Dua A", "01.234.567.8-901.000")
    b = await company(pool, "PT Synthetic Dua B", "012345678901000")
    with pytest.raises(HTTPException) as caught:
        await create(
            pool, person, invoice_company_name="Anything", invoice_company_npwp="0123 4567 8901 000"
        )
    assert caught.value.status_code == 409
    assert {c["id"] for c in caught.value.detail["candidates"]} == {a, b}
    assert await pool.fetchval("SELECT count(*) FROM practices") == 0


@pytest.mark.asyncio
async def test_link_is_written_once_even_without_the_unique_constraint(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "ALTER TABLE client_company_links DROP CONSTRAINT client_company_links_client_id_company_id_key"
    )
    person = await client(pool)
    first = await create(pool, person, invoice_company_name="PT Synthetic Sekali")
    await create(pool, person, invoice_company_name="PT Synthetic Sekali")
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM client_company_links WHERE client_id=$1 AND company_id=$2",
            person,
            first.invoice_company_id,
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["link_route", "practice_form"])
async def test_every_link_writer_waits_for_the_link_lock(pool: asyncpg.Pool, writer: str) -> None:
    from backend.app.modules.crm import company_router
    from backend.app.modules.crm.company_schemas import ClientCompanyLinkCreate
    from backend.services.invoicing.invoice_company import link_lock_key

    person = await client(pool)
    co = await company(pool, "PT Synthetic Kunci")

    async def write() -> object:
        if writer == "practice_form":
            return await create(pool, person, invoice_company_id=co)
        return await company_router.link_client_to_company(
            request=None,
            company_id=co,
            client_id=person,
            data=ClientCompanyLinkCreate(role="Director"),
            db=pool,
            current_user=USER,
        )

    async def links() -> int:
        return await pool.fetchval(
            "SELECT count(*) FROM client_company_links WHERE client_id=$1 AND company_id=$2",
            person,
            co,
        )

    async with pool.acquire() as holder:
        async with holder.transaction():
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", link_lock_key(person, co)
            )
            task = asyncio.create_task(write())
            await asyncio.sleep(0.5)
            assert not task.done()
            assert await links() == 0
    await task
    assert await links() == 1


@pytest.mark.asyncio
async def test_practice_reads_do_not_depend_on_the_company_table_or_the_new_column(
    pool: asyncpg.Pool,
) -> None:
    """The deploy window (code live, migration not yet applied) and bare CI schemas."""
    person = await client(pool)
    created = await create(pool, person)
    await pool.execute("ALTER TABLE practices DROP COLUMN invoice_company_id")
    await pool.execute("DROP TABLE client_company_links")
    await pool.execute("DROP TABLE companies")
    shown = await crm_practices.get_practice(
        request=None, practice_id=created.id, db_pool=pool, current_user=USER
    )
    assert shown["invoice_company_name"] is None
    patched = await crm_practices.update_practice(
        request=None,
        practice_id=created.id,
        updates=PracticeUpdate(priority="high"),
        db_pool=pool,
        current_user=USER,
    )
    assert patched["priority"] == "high"
