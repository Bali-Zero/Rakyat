"""Bridging: pinned extraction, NULL unknowns, exact catalog key and deterministic SQL."""

import hashlib
import json
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import pre_ruling_card

LAW_DIR = importer.HERE / "law" / "BRIDGING"
MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "342_visa_types_law_family_bridging.sql"
CARD_PIN = (
    "76aaaf5a43f89cfffc30a0abb7ae348936431d553e5b6345a14faf3cd7a8fa59"  # pragma: allowlist secret
)
PAGE_PIN = (
    "4504bdf2fb3576da5e3513d12540477a2e355bfd24ca757fb27246654cdcad63"  # pragma: allowlist secret
)


def test_entire_card_matches_canonical_extraction_after_formatting() -> None:
    card = json.loads((LAW_DIR / "BRIDGING.json").read_text())
    # The pin is the extraction; the 2026-10-19 owner-ruling additions are reversed first.
    canonical = json.dumps(
        pre_ruling_card(card), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == CARD_PIN
    assert card["page_sha256"] == PAGE_PIN
    assert {p.stem for p in LAW_DIR.glob("*.json")} == {"BRIDGING"}
    assert len(card["source_documents"]) == 4
    assert [doc["http_status"] for doc in card["source_documents"]] == [404, 200, 200, 200]
    assert card["source_documents"][1]["page_sha256"] == PAGE_PIN


def test_row_preserves_card_and_all_unresolved_fields() -> None:
    (row,) = importer.build_rows("BRIDGING")
    path = LAW_DIR / "BRIDGING.json"
    card = json.loads(path.read_text())
    assert row["code"] == row["family"] == row["permit_class"] == "BRIDGING"
    assert row["category"] == "Special Visa"
    assert row["law_card"] == card
    assert row["official_name_id"] == "Izin Tinggal Peralihan"
    assert row["source_url"] == card["source_url"]
    assert row["page_sha256"] == PAGE_PIN
    assert row["card_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["retrieved_at"] == card["retrieved_at"]
    assert row["verification_status"] == "VERIFIED"
    assert row["apply_channel"] == "ONSHORE"
    assert row["initial_stay_days"] == 60
    assert row["stay_options"] == [{"value": 60, "unit": "DAY"}]
    assert row["extendable"] is False  # owner ruling 2026-10-19, Pasal 86A ayat (1)
    for column in (
        "entry_type",
        "sponsor",
        "max_total_days",
        "max_total_months",
        "itap",
        "valid_from",
        "valid_until",
        "superseded_by",
    ):
        assert row[column] is None, column


def test_exact_price_key_and_no_pnbp_or_stamp_duty_as_client_price() -> None:
    (row,) = importer.build_rows("bridging")
    assert row["price_keys"] == [{"category": "single_entry_visas", "item_key": "Bridging Visa"}]
    assert row["is_sold"] is True
    product = importer.load_prices()["single_entry_visas"]["Bridging Visa"]
    assert not product.get("internal")
    assert row["law_card"]["pnbp_verbatim_for_reference_only"] == []
    assert not {"price", "cost_visa", "pnbp"} & row.keys()
    with pytest.raises(ValueError, match="not found in price list"):
        importer._check_price_keys(
            "BRIDGING",
            [{"category": "single_entry_visas", "item_key": "BRIDGING invented"}],
            importer.load_prices(),
        )


def test_migration_is_generated_guards_case_and_changes_law_columns_only(tmp_path: Path) -> None:
    sql = importer.emit_sql("BRIDGING")
    latest = MIGRATIONS / importer.latest_migration("BRIDGING")
    assert latest.name == "344_visa_types_law_rulings_2026_10_19.sql"
    forward, rollback = sql.split("-- === ROLLBACK ===", 1)
    assert forward.index("DO $guard$") < forward.index("INSERT INTO visa_types")
    assert "code <> upper(code)" in forward
    assert "upper(code) IN ('BRIDGING')" in forward
    assert forward.count("INSERT INTO visa_types") == 1
    assert "stay_options = EXCLUDED.stay_options" in forward
    assert "apply_channel = EXCLUDED.apply_channel" in forward
    for column in ("name", "category", "duration", "cost_visa", "requirements", "metadata"):
        assert f"{column} = EXCLUDED" not in forward
    assert "CREATE TABLE" not in forward and "ALTER TABLE" not in forward
    assert "DELETE FROM" not in rollback and "DROP" not in rollback
    assert "verification_status = 'TO_VERIFY'" in rollback
    assert "stay_options = NULL" in rollback
    assert "WHERE code IN ('BRIDGING')" in rollback
    assert importer.main(["--family", "BRIDGING", "--check", str(latest)]) == 0
    drift = tmp_path / latest.name
    drift.write_text(latest.read_text() + "-- drift\n")
    assert importer.main(["--family", "BRIDGING", "--check", str(drift)]) == 1


@pytest.mark.parametrize(
    "family,number", [("C", 331), ("E30", 333), ("E28", 335), ("E33", 336), ("E31", 337)]
)
def test_previous_family_migrations_are_byte_identical(family: str, number: int) -> None:
    path = MIGRATIONS / f"{number}_visa_types_law_family_{family.lower()}.sql"
    assert importer.FAMILY_MIGRATIONS[family][0] == path.name
    latest = MIGRATIONS / importer.latest_migration(family)
    assert importer.main(["--family", family, "--check", str(latest)]) == 0
    assert all(row["apply_channel"] is None for row in importer.build_rows(family))
