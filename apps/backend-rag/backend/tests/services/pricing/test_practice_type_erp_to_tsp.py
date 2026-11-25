"""DAI-176: the other_erp service is displayed as TSP (Termination Stay Permit).

Only the display name moves. The practice code, the catalogue key that
/api/pricing/service looks up, the icon and the 800.000 IDR price stay.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.db.migration_base import split_migration_sql

TSP_NAME = "TSP (Termination Stay Permit)"
CATALOGUE_KEY = "ERP (Exit Re-entry Permit)"
PRICE = "800.000 IDR"

_BACKEND = Path(__file__).resolve().parents[3]
_SHEET = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_MIRROR = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"
_MIGRATION = _BACKEND / "db" / "migrations_v2" / "378_practice_type_erp_to_tsp.sql"


@pytest.fixture(scope="module")
def pricing_service():
    # search_service appends the client-facing contact trailer, which imports app settings.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JWT_SECRET_KEY", "x" * 40)
        mp.setenv("API_KEYS", "test-key-for-pricing-search")
        from backend.services.pricing.pricing_service import PricingService

        service = PricingService()
        assert service.loaded
        yield service


def _sheet_row() -> dict:
    sheet = json.loads(_SHEET.read_text(encoding="utf-8"))
    return sheet["services"]["other_process"][CATALOGUE_KEY]


def _statements(sql: str) -> str:
    return re.sub(r"--.*$", "", sql, flags=re.MULTILINE)


def test_sheet_shows_tsp_and_keeps_key_price_and_icon():
    row = _sheet_row()
    assert row["name"] == TSP_NAME
    assert row["price"] == PRICE
    assert row["icon_id"] == "other-erp"


def test_public_mirror_shows_the_same_name_and_price():
    mirror = json.loads(_MIRROR.read_text(encoding="utf-8"))
    row = mirror["services_by_category"]["other_process"][CATALOGUE_KEY]
    assert row["key"] == CATALOGUE_KEY
    assert row["name"] == TSP_NAME
    assert row["price"] == PRICE


def test_exact_key_lookup_still_answers_with_the_new_name(pricing_service):
    row = pricing_service.get_service_by_key(CATALOGUE_KEY)
    assert row is not None
    assert row["name"] == TSP_NAME
    assert row["price"] == PRICE


@pytest.mark.parametrize(
    "query",
    ["TSP", "termination stay permit", "ERP", "exit re-entry"],
)
def test_search_finds_the_service_by_new_and_old_words(pricing_service, query):
    result = pricing_service.search_service(query)
    assert CATALOGUE_KEY in result.get("results", {}).get("other_process", {})


def test_llm_price_list_prints_tsp_and_not_the_old_label(pricing_service):
    context = pricing_service.format_for_llm_context()
    assert f"- {TSP_NAME}: {PRICE}" in context
    assert f"- {CATALOGUE_KEY}:" not in context


def test_llm_price_list_keeps_printing_other_rows_by_key(pricing_service):
    context = pricing_service.format_for_llm_context()
    assert "- EPO (Exit Permit Only): 700.000 IDR" in context
    assert "- C8A,B:" in context


def test_migration_updates_only_the_name_of_one_code():
    forward, rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
    body = _statements(forward)
    assert len(re.findall(r"\bUPDATE\b", body)) == 1
    assert f"SET name = '{TSP_NAME}'" in body
    assert "WHERE code = 'other_erp'" in body
    assert "AND name LIKE 'ERP%'" in body
    assert "RAISE EXCEPTION" in body
    assert not re.search(r"\b(ALTER|CREATE|DROP|TRUNCATE|DELETE|INSERT)\b", body)
    assert not re.search(r"\b(base_price|description|is_active|code\s*=\s*'[^o])", body)
    assert rollback is not None
    assert f"SET name = '{CATALOGUE_KEY}'" in rollback
    assert f"AND name = '{TSP_NAME}'" in rollback
