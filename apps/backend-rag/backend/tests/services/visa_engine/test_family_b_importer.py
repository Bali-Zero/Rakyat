"""Family B: source fidelity, exact catalog references and deterministic SQL."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import pre_ruling_card

FAMILY = "B"
ROWS = importer.build_rows(FAMILY)
BY_CODE = {r["code"]: r for r in ROWS}
MIGRATION = (
    Path(importer.__file__).parents[3] / "db" / "migrations_v2" / "340_visa_types_law_family_b.sql"
)
EXPECTED_PRICES = {
    "B1": [
        ("single_entry_visas", "B1 Visa on Arrival (VOA)"),
        ("single_entry_visas", "B1 Visa on Arrival Extension"),
    ]
}
SECONDARY = ()
PINS = {
    "B1": (
        "45c38dc51ce2ebca3643b37366626b81c82cc25ce6d8fc3b7e077ca6958664ce",  # pragma: allowlist secret
        "8515ba63329c14232dee61dcf56ab38f28b49646da0898eae05f302a5fdfd829",  # pragma: allowlist secret
    ),
    "B4": (
        "de02bb3433bd8b8042f762a416cf2c368bb74a4a26bc5929f925e0e066ae2052",  # pragma: allowlist secret
        "83b1996ef7aea8d231f7471c86df51e0f239e52f270de450225fa09dfebc64b9",  # pragma: allowlist secret
    ),
}


def test_exact_card_inventory() -> None:
    assert set(BY_CODE) == set(PINS)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["code"])
def test_complete_source_fidelity_and_projection(row: dict) -> None:
    code = row["code"]
    path = importer.HERE / "law" / FAMILY / f"{code}.json"
    raw = path.read_bytes()
    card = json.loads(raw)
    expected_page, expected_card = PINS[code]
    assert card["page_sha256"] == row["page_sha256"] == expected_page
    assert (
        hashlib.sha256(
            json.dumps(pre_ruling_card(card), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        == expected_card
    )
    assert row["card_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["law_card"] == card
    assert row["official_name_id"] == card["official_name_id"]
    assert (
        row["source_url"]
        == card["source_url"]
        == f"https://www.imigrasi.go.id/wna/daftar-visa-indonesia/{code}"
    )
    assert datetime.fromisoformat(row["retrieved_at"]) == datetime.fromisoformat(
        card["retrieved_at"]
    )
    assert row["family"] == FAMILY
    assert row["permit_class"] == (None if code in SECONDARY else "VOA")
    assert row["verification_status"] == ("TO_VERIFY" if code in SECONDARY else "VERIFIED")
    assert row["entry_type"] == (None if code in SECONDARY else "SINGLE")
    assert row["itap"] is None and row["apply_channel"] is None
    assert row["max_total_months"] is None
    assert row["valid_from"] is None and row["valid_until"] is None and row["superseded_by"] is None
    if code not in SECONDARY:
        assert row["stay_options"] == [{"value": row["initial_stay_days"], "unit": "DAY"}]
    else:
        for field in (
            "stay_options",
            "initial_stay_days",
            "sponsor",
            "extendable",
            "max_total_days",
            "is_sold",
        ):
            assert row[field] is None


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["code"])
def test_only_exact_price_keys_with_unknown_sale_status_otherwise(row: dict) -> None:
    expected = [
        {"category": cat, "item_key": key} for cat, key in EXPECTED_PRICES.get(row["code"], [])
    ]
    assert row["price_keys"] == expected
    assert row["is_sold"] is (True if expected else None)
    prices = importer.load_prices()
    for key in expected:
        assert key["item_key"] in prices[key["category"]]


def test_generated_sql_and_cli_check() -> None:
    sql = importer.emit_sql(FAMILY)
    # The applied family migration is immutable; the family is checked against its latest one.
    latest = MIGRATION.parent / importer.latest_migration(FAMILY)
    assert latest.name == "344_visa_types_law_rulings_2026_10_19.sql"
    assert importer.main(["--family", FAMILY, "--check", str(latest)]) == 0
    assert importer.main(["--check-latest"]) == 0
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
        "requirements",
        "metadata",
        "cost_visa",
    ):
        assert f"{column} = EXCLUDED" not in sql


@pytest.mark.parametrize(
    "family,number", [("C", 331), ("E30", 333), ("E28", 335), ("E33", 336), ("E31", 337)]
)
def test_preexisting_applied_output_remains_byte_identical(family: str, number: int) -> None:
    path = MIGRATION.parent / f"{number}_visa_types_law_family_{family.lower()}.sql"
    assert importer.FAMILY_MIGRATIONS[family][0] == path.name
    latest = MIGRATION.parent / importer.latest_migration(family)
    assert importer.main(["--family", family, "--check", str(latest)]) == 0
