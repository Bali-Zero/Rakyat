"""E23: pinned source fidelity, exact commercial keys, NULLs and migration 338."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer

ROWS = importer.build_rows("E23")
BY_CODE = {row["code"]: row for row in ROWS}
LAW_DIR = importer.HERE / "law" / "E23"
MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "338_visa_types_law_family_e23.sql"
SECONDARY = {"E23A", "E23U", "E23V", "E23X", "E23Y"}
PRICE_NAMES = {
    "Working KITAS (Altus/Onshore)",
    "Working KITAS (Offshore)",
    "Working KITAS (Extend)",
    "Freelance E23 (Altus/Onshore)",
    "Freelance E23 (Offshore)",
}
# Canonical hashes of the supplied cards BEFORE the two ITAP field aliases.
SOURCE_CONTENT_SHA256 = {
    "E23": "f5ee33e193217d4e649fc3979d7e0ec6080bdfc025d9c3a9d5e2c4e59f0f1aad",  # pragma: allowlist secret
    "E23A": "e252c48797572d2e1b8d5f2fb0a3266f69d0070e3646896ab350448f35bb6731",  # pragma: allowlist secret
    "E23U": "ffaf7918f932c9f583f24ce902e79b424f9f8aca3c5d6dce318eb32e05faa3ba",  # pragma: allowlist secret
    "E23V": "5029cbcf937767e7361a44df51d7f3076daa98f1b295002c7e38ca1c9806c2d5",  # pragma: allowlist secret
    "E23X": "cdef91b852165104fd70aa4bd5d34b28f9c4faaab9532b9326adbe24abb1ff0c",  # pragma: allowlist secret
    "E23Y": "cc5f3cbef96fbc77f9b65d646d01dc4b34f7d5aa3ac0a0dc1ee462076590fe37",  # pragma: allowlist secret
    "ITAP": "47ed2da16c55d41bb937ba51e238c33f9e21be70f43496112ed7cffa0e620cd2",  # pragma: allowlist secret
}


def test_six_official_codes_and_no_legacy_freelance_card() -> None:
    assert set(BY_CODE) == {"E23", *SECONDARY}
    assert {p.stem for p in LAW_DIR.glob("*.json")} == {*BY_CODE, "ITAP"}
    assert not (LAW_DIR / "E23-FREELANCE.json").exists()


@pytest.mark.parametrize("stem,expected", SOURCE_CONTENT_SHA256.items())
def test_all_supplied_fields_including_quotes_and_page_hashes_are_unchanged(
    stem: str, expected: str
) -> None:
    original = json.loads((LAW_DIR / f"{stem}.json").read_text())
    if stem != "ITAP":
        assert original.pop("itap_eligibility_as_stated_boolean") is None
        assert original.pop("itap_status") == "TO_VERIFY"
    canonical = json.dumps(
        original, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == expected


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["code"])
def test_projection_preserves_provenance_and_every_unknown(row: dict) -> None:
    secondary = row["code"] in SECONDARY
    assert row["family"] == "E23" and row["category"] == "KITAS/Limited Stay"
    assert row["permit_class"] == (None if secondary else "ITAS")
    assert row["entry_type"] == (None if secondary else "MULTIPLE")
    assert row["sponsor"] == (None if secondary else "REQUIRED")
    assert row["extendable"] is (None if secondary else True)
    assert row["verification_status"] == ("TO_VERIFY" if secondary else "VERIFIED")
    assert row["is_sold"] is (None if secondary else True)
    card = row["law_card"]
    for column in ("official_name_id", "source_url", "page_sha256", "retrieved_at"):
        assert row[column] == card[column]
    assert row["source_url"].endswith("/" + row["code"])
    path = LAW_DIR / f"{row['code']}.json"
    assert row["law_card"] == json.loads(path.read_text())
    assert row["card_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", row["page_sha256"])
    for column in (
        "initial_stay_days",
        "max_total_days",
        "max_total_months",
        "apply_channel",
        "itap",
        "valid_from",
        "valid_until",
        "superseded_by",
    ):
        assert row[column] is None
    if secondary:
        assert card["status"] == "NOT_FOUND" and card["not_found_reason"] == "Data Belum Tersedia"
        assert card["requirements_verbatim"] == []
        assert row["stay_options"] is None and row["price_keys"] == []
    else:
        assert row["stay_options"] == [
            {"value": 180, "unit": "DAY"},
            {"value": 1, "unit": "YEAR"},
            {"value": 2, "unit": "YEAR"},
        ]


def test_every_e23_commercial_key_is_linked_exactly_without_inventing_a_price() -> None:
    prices = importer.load_prices()
    keys = BY_CODE["E23"]["price_keys"]
    assert len(keys) == 5
    assert all(pk["category"] == "kitas_permits" for pk in keys)
    assert {pk["item_key"] for pk in keys} == PRICE_NAMES
    found = {
        key for key in prices["kitas_permits"] if re.search(r"working|freelance|\bE23", key, re.I)
    }
    assert found == PRICE_NAMES
    for pk in keys:
        assert prices[pk["category"]][pk["item_key"]]["price"]
    assert all(not row["price_keys"] for row in ROWS if row["code"] in SECONDARY)


def test_generated_sql_guard_rollback_and_no_legacy_column_updates(tmp_path) -> None:
    sql = importer.emit_sql("E23")
    # Each family is checked against its LATEST migration; for E23 that is still 338.
    assert importer.latest_migration("E23") == MIGRATION.name
    assert (
        MIGRATION.read_bytes()
        == importer.expected_sql(MIGRATION.name, "E23").encode()
        == sql.encode()
    )
    assert sql.count("INSERT INTO visa_types") == 6
    assert importer.main(["--family", "E23", "--check", str(MIGRATION)]) == 0
    drifted = tmp_path / "drift.sql"
    drifted.write_text(sql + "-- drift\n")
    assert importer.main(["--family", "E23", "--check", str(drifted)]) == 1
    guard = sql[sql.index("DO $guard$") : sql.index("$guard$;")]
    assert all(f"'{code}'" in guard for code in BY_CODE)
    assert sql.index("DO $guard$") < sql.index("INSERT INTO visa_types")
    assert "stay_options = EXCLUDED.stay_options" in sql
    rollback = sql.split("-- === ROLLBACK ===", 1)[1]
    assert "stay_options = NULL" in rollback
    assert "verification_status = 'TO_VERIFY'" in rollback
    assert "DELETE FROM" not in rollback and "E23-FREELANCE" not in sql
    for column in (
        "name",
        "category",
        "duration",
        "cost_visa",
        "cost_extension",
        "requirements",
        "metadata",
        "last_updated",
    ):
        assert f"{column} = EXCLUDED" not in sql


@pytest.mark.parametrize(
    "family,number", [("C", 331), ("E30", 333), ("E28", 335), ("E33", 336), ("E31", 337)]
)
def test_all_five_existing_generated_migrations_remain_byte_identical(
    family: str, number: int
) -> None:
    # Applied migrations are immutable; a family is checked against its LATEST migration
    # (344 for E33/E31 since the 2026-10-19 owner rulings).
    path = MIGRATIONS / f"{number}_visa_types_law_family_{family.lower()}.sql"
    assert importer.FAMILY_MIGRATIONS[family][0] == path.name
    latest = MIGRATIONS / importer.latest_migration(family)
    assert latest.read_bytes() == importer.expected_sql(latest.name, family).encode()
    assert importer.main(["--family", family, "--check", str(latest)]) == 0
