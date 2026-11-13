"""Unit tests for the practice and company smart-search builders (no database).

The SQL helpers ``bz_fold`` and ``bz_phone_core`` are replaced by small Python stand-ins on a fake
connection; the real functions are exercised on PostgreSQL in ``tests/db/test_entity_search_pg.py``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pytest

from backend.services.crm import entity_search
from backend.services.crm.entity_search import (
    build_company_search,
    build_practice_search,
    escape_like,
)


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-zЀ-ӿ]+", " ", plain).split())


def _core(digits: str) -> str:
    only = re.sub(r"\D", "", digits)
    for prefix in ("62", "0"):
        if only.startswith(prefix):
            return only[len(prefix) :]
    return only


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(sql)
        if "bz_fold" in sql:
            return _fold(args[0])
        if "bz_phone_core" in sql:
            return [_core(t) for t in args[0]]
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


class TestPracticeSearch:
    @pytest.mark.asyncio
    async def test_lowercase_unaccented_name_becomes_one_client_key_token(self, conn):
        built = await build_practice_search(conn, "müller", 4)
        assert built is not None
        assert built.params == ["muller"]
        assert (
            "public.bz_client_key(c.full_name, c.email) LIKE '%' || $4::text || '%'" in built.clause
        )
        assert "public.bz_name_key(pt.name)" in built.clause
        assert "public.bz_name_key(pt.code)" in built.clause
        assert built.next_index == 5

    @pytest.mark.asyncio
    async def test_token_order_is_free_every_token_must_match(self, conn):
        built = await build_practice_search(conn, "Rossi  MARIO", 1)
        assert built is not None
        assert built.params == ["rossi", "mario"]
        assert built.clause.count(" AND (") == 2

    @pytest.mark.asyncio
    async def test_phone_formats_share_one_core(self, conn):
        cores = []
        for typed in ("0812 3456 7890", "62 812 3456 7890", "+62 812-3456-7890"):
            built = await build_practice_search(conn, typed, 1)
            assert built is not None
            cores.append(built.params[-1])
            assert "public.bz_phone_key(c.phone, c.phone_normalized, c.whatsapp)" in built.clause
        assert cores == ["81234567890"] * 3

    @pytest.mark.asyncio
    async def test_numeric_token_also_matches_the_practice_number(self, conn):
        built = await build_practice_search(conn, "1234", 1)
        assert built is not None
        assert "p.id::text LIKE" in built.clause

    @pytest.mark.asyncio
    async def test_a_name_token_never_reaches_the_phone_or_number_arms(self, conn):
        built = await build_practice_search(conn, "rossi", 1)
        assert built is not None
        assert "bz_phone_key" not in built.clause
        assert "p.id::text" not in built.clause

    @pytest.mark.asyncio
    async def test_all_short_tokens_fall_back_to_legacy(self, conn):
        assert await build_practice_search(conn, "ab c", 1) is None

    @pytest.mark.asyncio
    async def test_short_token_next_to_long_one_still_filters(self, conn):
        built = await build_practice_search(conn, "ab rossi", 1)
        assert built is not None
        assert built.params == ["ab", "rossi"]

    @pytest.mark.asyncio
    async def test_punctuation_only_matches_nothing(self, conn):
        built = await build_practice_search(conn, "---", 3)
        assert built is not None
        assert built.clause == " AND FALSE"
        assert built.params == []
        assert built.next_index == 3

    @pytest.mark.asyncio
    async def test_user_text_is_only_ever_a_parameter(self, conn):
        built = await build_practice_search(conn, "x'; DROP TABLE practices;--", 1)
        assert built is not None
        assert "DROP" not in built.clause
        assert all(p.startswith("$") is False for p in built.params)


class TestCompanySearch:
    @pytest.mark.asyncio
    async def test_prefix_is_ignored_in_the_typed_text(self, conn):
        built = await build_company_search(conn, "PT Acme", 1)
        assert built is not None
        assert built.params == ["acme"]
        assert "public.bz_name_key(c.company_name)" in built.clause

    @pytest.mark.asyncio
    async def test_cv_prefix_and_tbk_suffix_are_ignored(self, conn):
        assert (await build_company_search(conn, "CV Acme Tbk", 1)).params == ["acme"]

    @pytest.mark.asyncio
    async def test_a_leading_accented_letter_survives_the_affix_rule(self, conn):
        for typed in ("\u00c1BC", "PT \u00c1BC", "cv \u00c1bc Tbk"):
            built = await build_company_search(conn, typed, 1)
            assert built is not None, typed
            assert built.params == ["abc"], typed

    @pytest.mark.asyncio
    async def test_non_ascii_words_stay_required_tokens(self, conn):
        # the fake fold keeps Cyrillic like the SQL bz_fold does (see PG test)
        for typed, expected in (
            ("тест Acme", ["тест", "acme"]),
            ("Acme тест", ["acme", "тест"]),
            ("PT тест", ["тест"]),
        ):
            built = await build_company_search(conn, typed, 1)
            assert built is not None and built.params == expected, typed

    @pytest.mark.asyncio
    async def test_an_affix_is_dropped_only_while_another_word_remains(self, conn):
        assert await build_company_search(conn, "PT", 1) is None
        assert await build_company_search(conn, "tbk", 1) is not None
        assert (await build_company_search(conn, "pt tbk", 1)).params == ["tbk"]
        assert (await build_company_search(conn, "ptb bayu", 1)).params == ["ptb", "bayu"]
        assert (await build_company_search(conn, "bayu cv", 1)).params == ["bayu", "cv"]

    @pytest.mark.asyncio
    async def test_plain_lowercase_name_matches_the_same_key(self, conn):
        assert (await build_company_search(conn, "acme", 1)).params == ["acme"]

    @pytest.mark.asyncio
    async def test_accents_and_case_are_folded(self, conn):
        assert (await build_company_search(conn, "BÄLI Fóods", 1)).params == ["bali", "foods"]

    @pytest.mark.asyncio
    async def test_digit_token_also_matches_nib_and_npwp(self, conn):
        built = await build_company_search(conn, "0000012345", 1)
        assert built is not None
        assert "public.bz_ident_key(c.nib, c.npwp_company)" in built.clause

    @pytest.mark.asyncio
    async def test_name_token_never_reaches_the_identifier_arm(self, conn):
        built = await build_company_search(conn, "acme", 1)
        assert "bz_ident_key" not in built.clause

    @pytest.mark.asyncio
    async def test_only_an_affix_falls_back_to_legacy_on_the_raw_text(self, conn):
        assert await build_company_search(conn, "PT", 1) is None

    @pytest.mark.asyncio
    async def test_all_short_tokens_fall_back_to_legacy(self, conn):
        assert await build_company_search(conn, "x", 1) is None

    @pytest.mark.asyncio
    async def test_params_are_numbered_from_the_start_index(self, conn):
        built = await build_company_search(conn, "acme bali", 3)
        assert built is not None
        assert "$3::text" in built.clause and "$4::text" in built.clause
        assert built.next_index == 5


class TestEscapeLike:
    def test_wildcards_are_literal(self):
        assert escape_like("50%_off\\") == "50\\%\\_off\\\\"

    def test_plain_text_is_unchanged(self):
        assert escape_like("acme") == "acme"


def test_module_reexports_the_availability_probe():
    assert entity_search.smart_search_available is not None
