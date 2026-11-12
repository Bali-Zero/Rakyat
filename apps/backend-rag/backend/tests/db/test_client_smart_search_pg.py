"""Smart client search on real PostgreSQL: word order, accents, punctuation, phones, linked PT.

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it). Synthetic
fixtures only. The function ``public.bz_fold`` is created by migration 358 in ``public`` (as
in production); the tables live in a per-test schema.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException  # noqa: F401  (kept for parity with sibling tests)

from backend.app.routers.crm_clients import list_clients
from backend.db.migration_base import split_migration_sql
from backend.phone_lock import phone_core
from backend.services.crm import client_search

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "db/migrations_v2/358_clients_smart_search_keys.sql"
)
ADMIN = {"email": "director@example.test", "role": "admin"}
STAFF = {"email": "staff@example.test", "role": "agent"}
OWNER = {"email": "owner@example.test", "role": "agent"}

_SCHEMA = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    phone_normalized TEXT,
    whatsapp TEXT,
    nationality TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    client_type TEXT NOT NULL DEFAULT 'individual',
    assigned_to TEXT,
    tax_consultant TEXT,
    avatar_url TEXT,
    first_contact_date TIMESTAMPTZ,
    last_interaction_date TIMESTAMPTZ,
    passport_number TEXT,
    passport_expiry DATE,
    date_of_birth DATE,
    gender TEXT,
    birthplace TEXT,
    company_name TEXT,
    custom_fields JSONB,
    address TEXT,
    notes TEXT,
    npwp TEXT,
    nib TEXT,
    tags TEXT[],
    ai_summary JSONB,
    ai_summary_generated_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE interactions (
    id SERIAL PRIMARY KEY,
    client_id INTEGER,
    sentiment TEXT,
    summary TEXT,
    interaction_date TIMESTAMPTZ
);
CREATE TABLE crm_guardian_summary_queue (
    id SERIAL PRIMARY KEY,
    client_id INTEGER,
    status TEXT,
    enqueued_at TIMESTAMPTZ
);
CREATE TABLE companies (
    id SERIAL PRIMARY KEY,
    company_name VARCHAR(255) NOT NULL,
    nib VARCHAR(100),
    npwp_company VARCHAR(100),
    status VARCHAR(50)
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    is_primary BOOLEAN DEFAULT FALSE,
    status VARCHAR(50) DEFAULT 'active'
);
"""


def up_sql(schema: str) -> str:
    up, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    return (
        up.replace("public.clients", f'"{schema}".clients')
        .replace("public.companies", f'"{schema}".companies')
        .replace(
            "table_schema = 'public' AND table_name", f"table_schema = '{schema}' AND table_name"
        )
    )


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    schema = "client_search_" + uuid4().hex
    created = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn, min_size=1, max_size=3, server_settings={"search_path": schema}
        )
        async with created.acquire() as conn:
            await conn.execute(_SCHEMA)
            await conn.execute(up_sql(schema))
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def add(pool: asyncpg.Pool, name: str, **cols: Any) -> int:
    names = ["full_name", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return await pool.fetchval(
        f"INSERT INTO clients ({', '.join(names)}) VALUES ({marks}) RETURNING id",
        name,
        *cols.values(),
    )


async def link_company(
    pool: asyncpg.Pool, client_id: int, name: str, nib: str | None = None, npwp: str | None = None
) -> int:
    company = await pool.fetchval(
        "INSERT INTO companies (company_name, nib, npwp_company, status)"
        " VALUES ($1, $2, $3, 'active') RETURNING id",
        name,
        nib,
        npwp,
    )
    await pool.execute(
        "INSERT INTO client_company_links (client_id, company_id) VALUES ($1, $2)",
        client_id,
        company,
    )
    return company


async def search(
    pool: asyncpg.Pool, q: str | None, user: dict | None = None, **kw: Any
) -> list[str]:
    rows = await list_clients(
        status=None,
        assigned_to=None,
        search=q,
        nationality=None,
        unnamed=False,
        passport_expiring_days=None,
        limit=kw.get("limit", 50),
        offset=kw.get("offset", 0),
        db_pool=pool,
        current_user=user or ADMIN,
    )
    return [r.full_name for r in rows]


async def book(pool: asyncpg.Pool) -> dict[str, int]:
    ids = {
        "rossi": await add(
            pool,
            "Mario Rossi",
            email="mario.rossi@example.test",
            phone="+62 812-3456-7890",
            nib="9120000000001",
            npwp="00.000.000.0-000.000",
        ),
        "muller_a": await add(pool, "Müller Anna"),
        "muller_h": await add(pool, "Muller Hans"),
        "strasse": await add(pool, "Straße Test"),
        "obrien": await add(pool, "O'Brien Pat"),
        "annemarie": await add(pool, "Anne-Marie Dupont"),
        "ivanov": await add(pool, "Иван Иванов"),
        "norm": await add(pool, "Phone Normalized Only", phone_normalized="0811 111 2222"),
        "wa": await add(pool, "Whatsapp Only", whatsapp="+62 812-2223333"),
        "plain": await add(pool, "Nobody Special"),
    }
    await link_company(
        pool, ids["rossi"], "PT Acme Synthetic", "9120000000002", "00.000.000.0-000.001"
    )
    await pool.execute("INSERT INTO companies (company_name) VALUES ('PT Zeta Unlinked')")
    return ids


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["rossi", "ROSSI", "Rossi", "  rossi  "])
async def test_casing_and_padding(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert await search(pool, typed) == ["Mario Rossi"]


@pytest.mark.asyncio
async def test_word_order_and_every_token_must_match(pool: asyncpg.Pool) -> None:
    await book(pool)
    assert await search(pool, "rossi mario") == ["Mario Rossi"]
    assert await search(pool, "mario rossi") == ["Mario Rossi"]
    assert await search(pool, "mario verdi") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["muller", "müller", "MÜLLER", "Muller"])
async def test_accents_match_in_both_directions(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert sorted(await search(pool, typed)) == ["Muller Hans", "Müller Anna"]


@pytest.mark.asyncio
async def test_sharp_s_and_cyrillic(pool: asyncpg.Pool) -> None:
    await book(pool)
    assert await search(pool, "strasse") == ["Straße Test"]
    assert await search(pool, "STRASSE") == ["Straße Test"]
    for typed in ("иванов", "ИВАНОВ", "Иван иванов"):
        assert await search(pool, typed) == ["Иван Иванов"], typed


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["obrien", "o brien", "o'brien", "O-Brien", "BRIEN"])
async def test_punctuation_in_names(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert await search(pool, typed) == ["O'Brien Pat"]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["annemarie", "anne marie", "Anne-Marie", "marie anne"])
async def test_hyphenated_names(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert await search(pool, typed) == ["Anne-Marie Dupont"]


@pytest.mark.asyncio
async def test_email_fragments(pool: asyncpg.Pool) -> None:
    await book(pool)
    assert await search(pool, "rossi example test") == ["Mario Rossi"]
    assert await search(pool, "mario.rossi@example.test") == ["Mario Rossi"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed",
    ["0812-3456", "+62 812 3456", "628123456", "812 3456", "(0812) 3456", "081234567890"],
)
async def test_phone_formats_are_the_same_number(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert await search(pool, typed) == ["Mario Rossi"]


@pytest.mark.asyncio
async def test_phone_normalized_and_whatsapp_columns_and_short_digit_floor(
    pool: asyncpg.Pool,
) -> None:
    await book(pool)
    assert await search(pool, "+62 811 111 2222") == ["Phone Normalized Only"]
    assert await search(pool, "0812 2223333") == ["Whatsapp Only"]
    # Fewer than three digits is not a phone fragment: the legacy raw substring search answers.
    assert await search(pool, "81") == ["Mario Rossi"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("typed", "found"),
    [
        ("9120000000001", "Mario Rossi"),
        ("00.000.000.0-000.000", "Mario Rossi"),
        ("000000000000000", "Mario Rossi"),
        ("9120000000002", "Mario Rossi"),
        ("00.000.000.0-000.001", "Mario Rossi"),
    ],
)
async def test_identifiers_of_the_client_and_the_linked_company(
    pool: asyncpg.Pool, typed: str, found: str
) -> None:
    await book(pool)
    assert await search(pool, typed) == [found]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["acme", "ACME", "synthetic acme", "mario acme", "pt acme"])
async def test_search_by_the_linked_company_name(pool: asyncpg.Pool, typed: str) -> None:
    await book(pool)
    assert await search(pool, typed) == ["Mario Rossi"]


@pytest.mark.asyncio
async def test_unlinked_company_and_unmatched_tokens_find_nobody(pool: asyncpg.Pool) -> None:
    await book(pool)
    assert await search(pool, "zeta") == []
    assert await search(pool, "acme verdi") == []


@pytest.mark.asyncio
async def test_ranking_exact_prefix_contains_other_then_recency(pool: asyncpg.Pool) -> None:
    other = await add(pool, "Luca Verdi", email="mario@example.test")
    contains = await add(pool, "Giulio Mario")
    prefix_old = await add(pool, "Mario Bianchi")
    prefix_new = await add(pool, "Mario Neri")
    exact = await add(pool, "Mario")
    await pool.execute(
        "UPDATE clients SET updated_at = now() - interval '10 days' WHERE id = $1", prefix_old
    )
    await pool.execute(
        "UPDATE clients SET updated_at = now() + interval '1 day' WHERE id = $1", other
    )
    names = await search(pool, "mario")
    assert names == ["Mario", "Mario Neri", "Mario Bianchi", "Giulio Mario", "Luca Verdi"]
    assert {exact, prefix_new, prefix_old, contains, other}
    paged = (
        await search(pool, "mario", limit=2, offset=0)
        + await search(pool, "mario", limit=2, offset=2)
        + await search(pool, "mario", limit=2, offset=4)
    )
    assert paged == names


@pytest.mark.asyncio
async def test_no_search_keeps_created_at_order_and_blank_or_punctuation_only(
    pool: asyncpg.Pool,
) -> None:
    first = await add(pool, "Older One")
    await add(pool, "Newer Two")
    await pool.execute(
        "UPDATE clients SET created_at = now() - interval '5 days' WHERE id = $1", first
    )
    assert await search(pool, None) == ["Newer Two", "Older One"]
    assert await search(pool, "   ") == ["Newer Two", "Older One"]
    assert await search(pool, "--") == []
    assert await search(pool, "...//") == []


@pytest.mark.asyncio
async def test_scope_and_soft_delete_bound_every_token_path(pool: asyncpg.Pool) -> None:
    ids = await book(pool)
    await pool.execute(
        "UPDATE clients SET assigned_to = $1 WHERE id = $2", OWNER["email"], ids["rossi"]
    )
    for typed in ("rossi", "0812 3456", "acme", "9120000000002", "mario acme"):
        assert await search(pool, typed, STAFF) == [], typed
        assert await search(pool, typed, OWNER) == ["Mario Rossi"], typed
    assert await search(pool, "muller", OWNER) == []
    await pool.execute("UPDATE clients SET deleted_at = now() WHERE id = $1", ids["rossi"])
    assert await search(pool, "rossi", ADMIN) == []
    assert await search(pool, "acme", ADMIN) == []


@pytest.mark.asyncio
async def test_results_follow_updates_and_no_column_is_added(pool: asyncpg.Pool) -> None:
    client = await add(pool, "Before Name", email="before@example.test", phone="0812 1111 2222")
    assert await search(pool, "before") == ["Before Name"]
    await pool.execute(
        "UPDATE clients SET full_name = 'Après Move', email = 'apres@example.test',"
        " phone = '+62 813 9999 0000' WHERE id = $1",
        client,
    )
    assert await search(pool, "before") == []
    assert await search(pool, "apres") == ["Après Move"]
    assert await search(pool, "0813-9999") == ["Après Move"]
    schema = await pool.fetchval("SELECT current_schema()")
    for table in ("clients", "companies"):
        derived = await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = $1"
            " AND table_name = $2 AND (is_generated = 'ALWAYS' OR column_name LIKE '%\\_key' ESCAPE '\\'"
            " OR column_name = 'name_words')",
            schema,
            table,
        )
        assert derived == [], (table, derived)


@pytest.mark.asyncio
async def test_migration_is_idempotent_guarded_and_immutable(pool: asyncpg.Pool) -> None:
    schema = await pool.fetchval("SELECT current_schema()")
    await pool.execute(up_sql(schema))
    assert await pool.fetchval(
        "SELECT provolatile FROM pg_proc WHERE proname = 'bz_fold' AND pronamespace = 'public'::regnamespace"
    ) in ("i", b"i")
    names = await pool.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND indexname LIKE '%\\_smart\\_%'"
        " ESCAPE '\\' ORDER BY 1",
        schema,
    )
    assert [r["indexname"] for r in names] == sorted(client_search.INDEX_NAMES)
    for fn in ("bz_name_key", "bz_client_key", "bz_phone_key", "bz_ident_key", "bz_phone_core"):
        assert await pool.fetchval(
            "SELECT provolatile FROM pg_proc WHERE proname = $1 AND pronamespace = 'public'::regnamespace",
            fn,
        ) in ("i", b"i")
    # A schema without the clients columns: the migration skips the keys and still succeeds.
    other = "client_search_guard_" + uuid4().hex[:8]
    await pool.execute(f'CREATE SCHEMA "{other}"')
    try:
        await pool.execute(up_sql(other))
    finally:
        await pool.execute(f'DROP SCHEMA "{other}" CASCADE')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "+62 812-3456-7890",
        "0812 3456 7890",
        "628123456789",
        "(021) 555-1234",
        "+1 415 555 0100",
        "812.3456.78",
    ],
)
async def test_phone_core_matches_the_dedup_projection(pool: asyncpg.Pool, raw: str) -> None:
    expected = phone_core(raw)
    got = await pool.fetchval("SELECT public.bz_phone_core($1::text)", raw)
    if expected is not None:
        assert got == expected
    else:
        assert len(got) < 6


@pytest.mark.asyncio
async def test_without_migration_the_legacy_substring_search_still_answers(
    pool: asyncpg.Pool,
) -> None:
    await book(pool)
    schema = await pool.fetchval("SELECT current_schema()")
    await pool.execute(f'DROP INDEX "{schema}".idx_clients_smart_key')
    assert await search(pool, "Mario") == ["Mario Rossi"]
    assert await search(pool, "9120000000002") == ["Mario Rossi"]


@pytest.mark.asyncio
async def test_only_the_first_six_tokens_count(pool: asyncpg.Pool) -> None:
    await book(pool)
    assert await search(pool, "mario rossi mario rossi mario rossi zzzz yyyy") == ["Mario Rossi"]
    assert await search(pool, "mario rossi mario rossi mario zzzz") == []


@pytest.mark.asyncio
async def test_ranking_exact_means_the_same_words_in_any_order(pool: asyncpg.Pool) -> None:
    await add(pool, "Mario Rossi Jr")
    await add(pool, "Rossi Mario Zed")
    await add(pool, "Mario Rossi")
    assert await search(pool, "rossi mario") == ["Mario Rossi", "Rossi Mario Zed", "Mario Rossi Jr"]


@pytest.mark.asyncio
async def test_exact_rank_needs_the_same_words_not_the_same_letters(pool: asyncpg.Pool) -> None:
    exact = await add(pool, "Ann Anna")
    other = await add(pool, "Annabel")
    await pool.execute(
        "UPDATE clients SET updated_at = now() + interval '1 day' WHERE id = $1", other
    )
    assert exact and other
    assert await search(pool, "ann anna") == ["Ann Anna", "Annabel"]


@pytest.mark.asyncio
async def test_prefix_rank_means_the_first_word_not_a_run_across_words(pool: asyncpg.Pool) -> None:
    await add(pool, "Mariorossi Zed")
    other = await add(pool, "Mario Rossi")
    await pool.execute(
        "UPDATE clients SET updated_at = now() + interval '1 day' WHERE id = $1", other
    )
    # "marior" starts the first word of the first client only; the second matches across the space.
    assert await search(pool, "marior") == ["Mariorossi Zed", "Mario Rossi"]


@pytest.mark.asyncio
async def test_exact_rank_needs_every_typed_word_as_a_whole_word(pool: asyncpg.Pool) -> None:
    await add(pool, "Mar Mario")
    other = await add(pool, "Mario")
    await pool.execute(
        "UPDATE clients SET updated_at = now() + interval '1 day' WHERE id = $1", other
    )
    assert await search(pool, "mar mario") == ["Mar Mario", "Mario"]


@pytest.mark.asyncio
async def test_when_every_token_is_under_three_characters_the_legacy_search_answers(
    pool: asyncpg.Pool,
) -> None:
    await book(pool)
    # Legacy = raw substring: "mu" finds the unaccented name but not "Müller" (no folding yet),
    # while the third character switches to the smart search and finds both.
    assert await search(pool, "mu") == ["Muller Hans"]
    assert sorted(await search(pool, "mul")) == ["Muller Hans", "Müller Anna"]
    assert await search(pool, "ma ri") == []  # raw "ma ri" is no substring of any field
    assert await search(pool, "rossi ma") == ["Mario Rossi"]  # long token present: smart


@pytest.mark.asyncio
async def test_a_short_token_beside_a_long_one_filters_name_email_and_linked_company(
    pool: asyncpg.Pool,
) -> None:
    await book(pool)
    assert await search(pool, "ma rossi") == ["Mario Rossi"]
    assert await search(pool, "zz rossi") == []
    assert await search(pool, "sy acme") == ["Mario Rossi"]  # "sy" only inside the PT name
    assert await search(pool, "zz acme") == []


async def _plan(pool: asyncpg.Pool, typed: str) -> str:
    async with pool.acquire() as conn:
        built = await client_search.build_client_search(conn, typed, 1)
        assert built is not None
        # Tiny tables: force the planner to pick among the INDEX paths (a bitmap scan on the
        # expression index) instead of a one-row-cheaper seq scan or full pkey scan.
        await conn.execute("SET enable_seqscan = off")
        await conn.execute("SET enable_indexscan = off")
        rows = await conn.fetch(
            "EXPLAIN SELECT c.id FROM clients c WHERE c.deleted_at IS NULL" + built.clause,
            *built.params[:-1],  # the last param belongs to the ORDER BY,
        )
    return "\n".join(r[0] for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("typed", "indexes"),
    [
        ("rossi", ["idx_clients_smart_key"]),
        ("acme", ["idx_clients_smart_key", "idx_companies_smart_name"]),
        (
            "0812-3456-7890",
            [
                "idx_clients_smart_key",
                "idx_clients_smart_phone",
                "idx_clients_smart_ident",
                "idx_companies_smart_name",
                "idx_companies_smart_ident",
            ],
        ),
    ],
)
async def test_the_query_is_served_by_the_expression_indexes(
    pool: asyncpg.Pool, typed: str, indexes: list[str]
) -> None:
    await book(pool)
    plan = await _plan(pool, typed)
    for index in indexes:
        assert index in plan, plan
    # A refactor to `IN (SELECT ...)` or to a different expression leaves a seq scan on clients.
    assert "Seq Scan on clients" not in plan, plan


@pytest.mark.asyncio
async def test_the_ascii_fast_path_folds_exactly_like_the_full_path(pool: asyncpg.Pool) -> None:
    samples = [
        "",
        "Mario Rossi",
        "O'Brien-Smith, Jr.",
        "a.b_c@Example.TEST",
        "  Tabs\tand   spaces ",
        'ABC xyz 0123 !"#$%&()*+,-./:;<=>?@[\\]^_`{|}~',
        "Müller",
        "Иван",
    ]
    for text in samples:
        fast, full = await pool.fetchrow(
            "SELECT public.bz_fold($1::text), public.bz_fold_full($1::text)", text
        )
        assert fast == full, text


@pytest.mark.asyncio
async def test_a_short_token_never_borrows_an_unrelated_company_identifier(
    pool: asyncpg.Pool,
) -> None:
    await book(pool)
    # "12" is inside the linked company's NIB, but the client "Nobody Special" has no company,
    # and a token under three digits is never an identifier fragment.
    assert await search(pool, "nobody 12") == []
    assert await search(pool, "rossi 12") == []  # "12" is in his phone only as a digit run
    assert await search(pool, "rossi ma") == ["Mario Rossi"]


@pytest.mark.asyncio
async def test_the_key_functions_do_not_depend_on_session_settings(pool: asyncpg.Pool) -> None:
    samples = [
        "Mario Rossi",
        "O'Brien-Smith, Jr.",
        "Müller \\ [x] a.b_c@Example.TEST",
        "Straße ŒUVRE İSTANBUL I",
        "Иван Иванов",
        "no\u00a0break  space",
    ]
    queries = [
        "SELECT public.bz_fold($1::text), public.bz_name_key($1::text),"
        " public.bz_client_key($1::text, $1::text), public.bz_phone_key($1::text, $1::text, $1::text),"
        " public.bz_ident_key($1::text, $1::text), public.bz_name_rank($1::text, ARRAY['mario'])"
    ]
    settings = [
        {"standard_conforming_strings": "off"},
        {"search_path": "pg_temp"},
        {"escape_string_warning": "off", "lc_messages": "C"},
    ]
    dsn = os.environ["NUZ_UNIVERSAL_PG_DSN"]
    async with pool.acquire() as conn:
        baseline = {t: [tuple(await conn.fetchrow(q, t)) for q in queries] for t in samples}
    # A fresh connection per setting: the function bodies are parsed again under that setting.
    for setting in settings:
        fresh = await asyncpg.connect(dsn, server_settings=setting)
        try:
            for text in samples:
                got = [tuple(await fresh.fetchrow(q, text)) for q in queries]
                assert got == baseline[text], (setting, text)
        finally:
            await fresh.close()
    # Case folding is explicit: a dotted capital I folds to "i" in both paths.
    folded = await pool.fetchval("SELECT public.bz_fold($1::text)", "ISTANBUL İstanbul")
    assert folded == "istanbul istanbul"


async def _generic_plan(pool: asyncpg.Pool, typed: str) -> str:
    async with pool.acquire() as conn:
        built = await client_search.build_client_search(conn, typed, 1)
        assert built is not None
        types = ["text[]" if isinstance(p, list) else "text" for p in built.params]

        def literal(value: Any) -> str:
            if isinstance(value, list):
                return "ARRAY[" + ", ".join(literal(v) for v in value) + "]::text[]"
            return "'" + str(value).replace("'", "''") + "'"

        await conn.execute("SET enable_seqscan = off")
        await conn.execute("SET enable_indexscan = off")
        await conn.execute("SET plan_cache_mode = force_generic_plan")
        await conn.execute(
            f"PREPARE gp({', '.join(types)}) AS SELECT c.id FROM clients c"
            " WHERE c.deleted_at IS NULL" + built.clause + " ORDER BY " + built.order_by
        )
        rows = await conn.fetch(
            f"EXPLAIN EXECUTE gp({', '.join(literal(p) for p in built.params)})"
        )
        await conn.execute("DEALLOCATE gp")
    return "\n".join(r[0] for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["rossi", "acme", "0812-3456-7890", "mar rossi"])
async def test_a_generic_plan_still_uses_the_expression_indexes(
    pool: asyncpg.Pool, typed: str
) -> None:
    await book(pool)
    plan = await _generic_plan(pool, typed)
    assert "idx_clients_smart_key" in plan, plan
    assert "Seq Scan on clients" not in plan, plan
