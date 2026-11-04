"""E31 Law projection: unknowns, exact catalog references and deterministic migration 337."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer

ROWS = importer.build_rows("E31")
BY_CODE = {r["code"]: r for r in ROWS}
MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "337_visa_types_law_family_e31.sql"
SPOUSES = {"E31A", "E31B"}
DEPENDENTS = {"E31E", "E31H", "E31J"}


def test_ten_codes_with_secondary_e31() -> None:
    assert set(BY_CODE) == {
        "E31",
        "E31A",
        "E31B",
        "E31C",
        "E31D",
        "E31E",
        "E31F",
        "E31G",
        "E31H",
        "E31J",
    }
    row = BY_CODE["E31"]
    assert row["verification_status"] == "TO_VERIFY"
    assert row["law_card"]["status"] == "NOT_FOUND"
    for key in ("permit_class", "sponsor", "entry_type", "extendable", "stay_options", "is_sold"):
        assert row[key] is None
    assert row["price_keys"] == []


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["code"])
def test_rows_preserve_source_and_unknown_fields(row: dict) -> None:
    missing = row["code"] == "E31"
    assert row["family"] == "E31"
    assert row["category"] == "KITAS/Limited Stay"
    assert row["permit_class"] == (None if missing else "ITAS")
    assert row["sponsor"] == (None if missing else "REQUIRED")
    assert row["entry_type"] == (None if missing else "MULTIPLE")
    assert row["extendable"] is (None if missing else True)
    assert row["verification_status"] == ("TO_VERIFY" if missing else "VERIFIED")
    for field in (
        "apply_channel",
        "initial_stay_days",
        "max_total_days",
        "max_total_months",
        "itap",
        "valid_from",
        "valid_until",
        "superseded_by",
    ):
        assert row[field] is None
    path = importer.HERE / "law" / "E31" / f"{row['code']}.json"
    assert row["card_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    card = json.loads(path.read_text())
    assert row["law_card"] == card
    assert row["official_name_id"] == card["official_name_id"]
    assert row["retrieved_at"] == card["retrieved_at"]
    assert row["source_url"] == card["source_url"]
    assert row["source_url"].endswith("/" + row["code"])
    assert row["page_sha256"] == card["page_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", row["page_sha256"])
    assert row["stay_options"] == (
        None if missing else [{"value": 1, "unit": "YEAR"}, {"value": 2, "unit": "YEAR"}]
    )


@pytest.mark.parametrize("code", sorted(SPOUSES | DEPENDENTS))
def test_commercial_mappings_use_exact_catalog_descriptions(code: str) -> None:
    label = "Spouse" if code in SPOUSES else "Dependent"
    expected = [
        {"category": "kitas_permits", "item_key": f"{label} {duration} ({channel})"}
        for duration in ("1 Year", "2 Years")
        for channel in ("Altus/Onshore", "Offshore", "Extend")
    ]
    assert BY_CODE[code]["price_keys"] == expected
    assert BY_CODE[code]["is_sold"] is True
    prices = importer.load_prices()
    for key in expected:
        item = prices[key["category"]][key["item_key"]]
        assert "price" in item
        if "Extend" not in key["item_key"]:
            description = item["description_en"]
            assert "KITAS/KITAP holder" in description
            if code in SPOUSES:
                assert "Indonesian citizens" in description


@pytest.mark.parametrize("code", ["E31C", "E31D", "E31F", "E31G"])
def test_owner_ruling_links_exact_dependent_keys(code: str) -> None:
    # Owner ruling 2026-10-19 supersedes the earlier unpriced ruling.
    prices = importer.load_prices()["kitas_permits"]
    keys = [(p["category"], p["item_key"]) for p in BY_CODE[code]["price_keys"]]
    assert keys == [
        ("kitas_permits", f"Dependent {term} ({channel})")
        for term in ("1 Year", "2 Years")
        for channel in ("Altus/Onshore", "Offshore", "Extend")
    ]
    assert all(item in prices for _category, item in keys)
    assert BY_CODE[code]["price_keys"] == BY_CODE["E31E"]["price_keys"]
    assert BY_CODE[code]["is_sold"] is True
    assert all(
        "KITAP" not in p["item_key"] and "Born" not in p["item_key"]
        for r in ROWS
        for p in r["price_keys"]
    )


def test_source_ambiguities_survive_projection_verbatim() -> None:
    expected = {
        "E31A": {"PNBP_COMPONENT_MISMATCH", "GCI_DIRECT_ITAP_VS_VISA_PAGE_ITAS"},
        "E31B": {"SPOUSE_NATIONALITY_MISMATCH", "GCI_DIRECT_ITAP_VS_VISA_PAGE_ITAS"},
        "E31C": {"GCI_DIRECT_ITAP_VS_VISA_PAGE_ITAS"},
    }
    for code, issues in expected.items():
        card = BY_CODE[code]["law_card"]
        assert {issue["id"] for issue in card["verification_issues"]} == issues
        assert all(issue["status"] == "TO_VERIFY" for issue in card["verification_issues"])
    b = BY_CODE["E31B"]["law_card"]
    assert "Pemegang ITAS/ITAP" in b["official_name_id"]
    assert "suami/istri warga negara Indonesia" in b["requirements_verbatim"][0]["text"]
    assert BY_CODE["E31D"]["law_card"]["requirements_verbatim"][8]["context_verbatim"]


def test_migration_is_generated_and_leaves_legacy_columns_untouched() -> None:
    sql = importer.emit_sql("E31")
    assert importer.latest_migration("E31") == "344_visa_types_law_rulings_2026_10_19.sql"
    assert sql.count("INSERT INTO visa_types") == 10
    guard = sql[sql.index("DO $guard$") : sql.index("$guard$;")]
    assert "code <> upper(code)" in guard
    assert all(f"'{code}'" in guard for code in BY_CODE)
    assert sql.index("DO $guard$") < sql.index("INSERT INTO visa_types")
    assert "stay_options = EXCLUDED.stay_options" in sql
    rollback = sql.split("-- === ROLLBACK ===", 1)[1]
    assert "stay_options = NULL" in rollback
    assert "verification_status = 'TO_VERIFY'" in rollback
    for column in (
        "name",
        "category",
        "duration",
        "extensions",
        "total_stay",
        "renewable",
        "requirements",
        "restrictions",
        "metadata",
        "cost_visa",
        "cost_extension",
    ):
        assert f"{column} = EXCLUDED" not in sql


def test_cli_check_and_previous_families_remain_byte_identical() -> None:
    for family, filename in (
        ("E31", "344_visa_types_law_rulings_2026_10_19.sql"),
        ("C", "331_visa_types_law_family_c.sql"),
        ("E30", "333_visa_types_law_family_e30.sql"),
    ):
        assert importer.latest_migration(family) == filename
        assert importer.main(["--family", family, "--check", str(MIGRATIONS / filename)]) == 0
