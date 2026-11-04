"""Family D: source fidelity, exact catalog references and deterministic SQL."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import pre_ruling_card

FAMILY = "D"
ROWS = importer.build_rows(FAMILY)
BY_CODE = {r["code"]: r for r in ROWS}
MIGRATION = (
    Path(importer.__file__).parents[3] / "db" / "migrations_v2" / "341_visa_types_law_family_d.sql"
)
EXPECTED_PRICES = {
    "D1": [
        ("multiple_entry_visas", "D1 Tourism (1 Year)"),
        ("multiple_entry_visas", "D1 Tourism (2 Years)"),
        ("multiple_entry_visas", "D1 Tourism (5 Years)"),
    ],
    "D2": [
        ("multiple_entry_visas", "D2 Business (1 Year)"),
        ("multiple_entry_visas", "D2 Business (2 Years)"),
        ("multiple_entry_visas", "D2 Business (5 Years)"),
    ],
    "D12": [
        ("multiple_entry_visas", "D12 Business Investigation (1 Year)"),
        ("multiple_entry_visas", "D12 Business Investigation (2 Years)"),
    ],
}
SECONDARY = ("D4", "D8")
PINS = {
    "D1": (
        "bff8e3254c9a0cc2b4905a2bbbd0d9ed519a56d7fbda142c454f7f3d65dcabb7",  # pragma: allowlist secret
        "cee12b530c5b00ce51669737090e1eaf9d98ea1bf71e426ffb972b48202e018e",  # pragma: allowlist secret
    ),
    "D12": (
        "2aefc8353dd1c9a085ef2cefb8895cb90b4508d0bdb3004a5eb047f9ae757f66",  # pragma: allowlist secret
        "7489a4b979596cd9ef3f9f3356b9816baac526eb32da3fe762387d9f576050b8",  # pragma: allowlist secret
    ),
    "D14": (
        "4b8507e95c61be4732193484f546696c57d6550b8e155f3947fbb1bfac45ee9f",  # pragma: allowlist secret
        "145316579621d7f14a911bf8095c76b002a6354b268a4ad06dd8519efc3bc62f",  # pragma: allowlist secret
    ),
    "D17": (
        "d5be487556d94c0c7fcfb8f293e626f627ce913811a0bc392be99cfc12b56861",  # pragma: allowlist secret
        "3263465cad9723eb545126517faaad60db7d81f3b9b616caac5b79223ee1d4f4",  # pragma: allowlist secret
    ),
    "D2": (
        "0090252998e744ddecedb17494ed17eee8e3ee8237a2cf23817241ad0bdb0185",  # pragma: allowlist secret
        "c3c5ee3c35516d791470d2e381560cb99f7e20c2b43ac84ddd02300b7d0e5671",  # pragma: allowlist secret
    ),
    "D3": (
        "206434a173a9674f8829776728cc2a5c0265d60f2da6fc0519275be48bce1b34",  # pragma: allowlist secret
        "5bbe8c8ff210cd02606017cc22d04f43f6834e668b319b0387016647b4aac49f",  # pragma: allowlist secret
    ),
    "D4": (
        "d1b158d17ebd5f211aeb3ac42d23a4c3bb40c843f726f1512eb007d9314e6b3b",  # pragma: allowlist secret
        "ada2faeb6cc2e165596f89bb9fe7f6bc36b116dc7ce0a58b48f1d65d2e14ce0a",  # pragma: allowlist secret
    ),
    "D7": (
        "7cbb491e49627990421f4462aa4c5512482c81a8fff4de9cc11d0e767ed7e72b",  # pragma: allowlist secret
        "987c480a5a1adf03872e95c4e3b602c4adf1fe276051d1b57280a2be1360fb3a",  # pragma: allowlist secret
    ),
    "D8": (
        "82f129b6004f93602d2e4308115ee934f50ff4a8160507dbaab2cf39d2f61f77",  # pragma: allowlist secret
        "9112a95c1f26d4a40908f22fec4521d72fd37a3bc5ce6f424e001041e09143f7",  # pragma: allowlist secret
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
    assert row["permit_class"] == (None if code in SECONDARY else "VISIT_MULTIPLE")
    assert row["verification_status"] == ("TO_VERIFY" if code in SECONDARY else "VERIFIED")
    assert row["entry_type"] == (None if code in SECONDARY else "MULTIPLE")
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
