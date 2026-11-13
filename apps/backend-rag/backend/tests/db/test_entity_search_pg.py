"""Smart practice (Process) and company search on real PostgreSQL.

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it). Synthetic
fixtures only. Reuses the schema and migration-358 setup of the client search test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from backend.app.modules.crm.company_router import list_companies
from backend.app.routers.crm_practices import list_practices
from backend.tests.db.test_client_smart_search_pg import (  # noqa: F401  (pool is a fixture)
    ADMIN,
    add,
    link_company,
    pool,
)

pytestmark = pytest.mark.integration

_PRACTICE_SCHEMA = """
CREATE TABLE practice_types (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT
);
CREATE TABLE client_family_members (
    id SERIAL PRIMARY KEY,
    full_name TEXT,
    relationship TEXT
);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    practice_type_id INTEGER NOT NULL REFERENCES practice_types(id),
    family_member_id INTEGER REFERENCES client_family_members(id),
    status TEXT NOT NULL DEFAULT 'inquiry',
    priority TEXT NOT NULL DEFAULT 'normal',
    assigned_to TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@pytest_asyncio.fixture
async def book(pool: asyncpg.Pool) -> AsyncIterator[dict[str, int]]:  # noqa: F811
    await pool.execute(_PRACTICE_SCHEMA)
    visa = await pool.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('kitas_investor', 'KITAS Investor')"
        " RETURNING id"
    )
    tax = await pool.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('npwp_personal', 'Pelaporan Pajak')"
        " RETURNING id"
    )
    anna = await add(pool, "Müller Anna", email="anna@example.test", phone="+62 812-3456-7890")
    hans = await add(pool, "Muller Hans", phone="0813 1111 2222")
    mario = await add(pool, "Mario Rossi", email="mario.rossi@example.test")

    async def practice(client: int, kind: int) -> int:
        return await pool.fetchval(
            "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
            client,
            kind,
        )

    ids = {
        "anna_visa": await practice(anna, visa),
        "hans_tax": await practice(hans, tax),
        "mario_visa": await practice(mario, visa),
        "mario_tax": await practice(mario, tax),
    }
    yield ids


async def find(pool: asyncpg.Pool, q: str | None, **kw: Any) -> set[int]:  # noqa: F811
    rows = await list_practices(
        client_id=None,
        status=None,
        assigned_to=None,
        practice_type=None,
        priority=None,
        month=None,
        search=q,
        include_history=False,
        limit=kw.get("limit", 50),
        offset=0,
        db_pool=pool,
        current_user=ADMIN,
    )
    return {r["id"] for r in rows}


class TestPracticeSearch:
    @pytest.mark.asyncio
    async def test_client_name_lowercase_without_accents(self, pool, book):  # noqa: F811
        assert await find(pool, "muller") == {book["anna_visa"], book["hans_tax"]}

    @pytest.mark.asyncio
    async def test_accented_query_matches_the_same(self, pool, book):  # noqa: F811
        assert await find(pool, "MÜLLER anna") == {book["anna_visa"]}

    @pytest.mark.asyncio
    async def test_token_order_is_free(self, pool, book):  # noqa: F811
        assert await find(pool, "rossi mario") == await find(pool, "mario rossi")
        assert await find(pool, "rossi mario") == {book["mario_visa"], book["mario_tax"]}

    @pytest.mark.asyncio
    async def test_client_and_service_tokens_combine(self, pool, book):  # noqa: F811
        assert await find(pool, "mario kitas") == {book["mario_visa"]}
        assert await find(pool, "pajak rossi") == {book["mario_tax"]}

    @pytest.mark.asyncio
    async def test_phone_formats_are_one_number(self, pool, book):  # noqa: F811
        for typed in ("0812 3456 7890", "6281234567890", "+62 812-3456-7890"):
            assert await find(pool, typed) == {book["anna_visa"]}, typed

    @pytest.mark.asyncio
    async def test_email_fragment(self, pool, book):  # noqa: F811
        assert await find(pool, "mario.rossi@example") == {book["mario_visa"], book["mario_tax"]}

    @pytest.mark.asyncio
    async def test_practice_number(self, pool, book):  # noqa: F811
        assert book["hans_tax"] in await find(pool, str(book["hans_tax"]))

    @pytest.mark.asyncio
    async def test_short_query_keeps_the_legacy_substring_search(self, pool, book):  # noqa: F811
        assert await find(pool, "ro") == {book["mario_visa"], book["mario_tax"]}

    @pytest.mark.asyncio
    async def test_wildcards_are_literal_in_the_legacy_path(self, pool, book):  # noqa: F811
        assert await find(pool, "%") == set()
        assert await find(pool, "_") == set()

    @pytest.mark.asyncio
    async def test_no_search_returns_all(self, pool, book):  # noqa: F811
        assert await find(pool, None) == set(book.values())

    @pytest.mark.asyncio
    async def test_nothing_matches_nothing(self, pool, book):  # noqa: F811
        assert await find(pool, "zzzzzz") == set()


_COMPANY_COLUMNS = (
    "uuid TEXT, brand_name TEXT, company_type TEXT, kbli_code TEXT, kbli_description TEXT,"
    " akta_pendirian_no TEXT, akta_pendirian_date DATE, akta_perubahan_no TEXT,"
    " akta_perubahan_date DATE, sk_menhumkam_no TEXT, sk_menhumkam_date DATE,"
    " registered_address TEXT, office_address TEXT, city TEXT, province TEXT, postal_code TEXT,"
    " company_phone TEXT, company_email TEXT, setup_progress TEXT, google_drive_folder_id TEXT,"
    " custom_fields JSONB, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, created_by TEXT"
).split(", ")


async def companies(pool: asyncpg.Pool, q: str | None, **kw: Any) -> list[str]:  # noqa: F811
    for column in _COMPANY_COLUMNS:
        await pool.execute(f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS {column}")
    rows = await list_companies(
        request=None,
        search=q,
        status=kw.get("status"),
        kbli=None,
        skip=0,
        limit=50,
        db=pool,
        current_user=ADMIN,
    )
    return [r["company_name"] for r in rows]


@pytest_asyncio.fixture
async def firms(pool: asyncpg.Pool) -> None:  # noqa: F811
    holder = await add(pool, "Holder Synthetic")
    await link_company(pool, holder, "PT Acme Synthetic", nib="9120000000001")
    await link_company(pool, holder, "CV Bali Foods", npwp="00.111.222.3-444.000")
    await link_company(pool, holder, "PT Kéy Ünit Tbk")
    await link_company(pool, holder, "PT X")
    await link_company(pool, holder, "Delta Mining")
    await link_company(pool, holder, "ABC Trading")
    await link_company(pool, holder, "Тест Zeta")


class TestCompanySearch:
    @pytest.mark.asyncio
    async def test_lowercase_name_finds_the_pt(self, pool, firms):  # noqa: F811
        assert await companies(pool, "acme") == ["PT Acme Synthetic"]

    @pytest.mark.asyncio
    async def test_typed_prefix_is_ignored(self, pool, firms):  # noqa: F811
        assert await companies(pool, "PT Acme") == ["PT Acme Synthetic"]
        assert await companies(pool, "cv bali") == ["CV Bali Foods"]

    @pytest.mark.asyncio
    async def test_typed_affix_finds_a_name_stored_without_it(self, pool, firms):  # noqa: F811
        assert await companies(pool, "PT Delta") == ["Delta Mining"]
        assert await companies(pool, "CV Delta Mining Tbk") == ["Delta Mining"]

    @pytest.mark.asyncio
    async def test_a_leading_accented_letter_is_not_dropped(self, pool, firms):  # noqa: F811
        assert await companies(pool, "\u00c1BC") == ["ABC Trading"]
        assert await companies(pool, "PT \u00c1BC trading") == ["ABC Trading"]

    @pytest.mark.asyncio
    async def test_non_ascii_words_are_required_and_the_prefix_is_still_ignored(
        self,
        pool,
        firms,  # noqa: F811
    ):
        expected = ["Тест Zeta"]
        assert await companies(pool, "тест zeta") == expected
        assert await companies(pool, "zeta тест") == expected
        assert await companies(pool, "PT тест") == expected
        assert await companies(pool, "тест delta") == []

    @pytest.mark.asyncio
    async def test_accent_and_case_insensitive(self, pool, firms):  # noqa: F811
        assert await companies(pool, "KEY unit") == ["PT Kéy Ünit Tbk"]

    @pytest.mark.asyncio
    async def test_token_order_is_free(self, pool, firms):  # noqa: F811
        assert await companies(pool, "foods bali") == ["CV Bali Foods"]

    @pytest.mark.asyncio
    async def test_nib_and_npwp_digits(self, pool, firms):  # noqa: F811
        assert await companies(pool, "9120000000001") == ["PT Acme Synthetic"]
        assert await companies(pool, "001112223444000") == ["CV Bali Foods"]

    @pytest.mark.asyncio
    async def test_single_letter_keeps_the_legacy_substring_search(self, pool, firms):  # noqa: F811
        assert "PT X" in await companies(pool, "x")

    @pytest.mark.asyncio
    async def test_wildcards_are_literal(self, pool, firms):  # noqa: F811
        assert await companies(pool, "%") == []

    @pytest.mark.asyncio
    async def test_no_search_lists_all(self, pool, firms):  # noqa: F811
        assert len(await companies(pool, None)) == 7
