"""E28 source integrity, projection, prices and generated migration isolation."""

import hashlib
import json
import re

import pytest

from backend.services.visa_engine.families import importer

ROWS = importer.build_rows("E28")
BY_CODE = {r["code"]: r for r in ROWS}
LAW_DIR = importer.HERE / "law" / "E28"
MIGRATIONS = importer.HERE.parents[2] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "335_visa_types_law_family_e28.sql"
SECONDARY = {"E28", "E28E"}
PRICE_KEYS = [
    {"category": "kitas_permits", "item_key": "Investor KITAS 2 Years (Altus/Onshore)"},
    {"category": "kitas_permits", "item_key": "Investor KITAS 2 Years (Offshore)"},
    {"category": "kitas_permits", "item_key": "Investor KITAS 2 Years (Extend)"},
]

# Hashes of the supplied source cards after canonical JSON serialization. Formatting
# may change; no field, quotation, requirement alternative or provenance may change.
SOURCE_CONTENT_SHA256 = {
    "E28": "d1f14a5c0dcbd5543a780f0b7a9120079ad04000321a8f3e1dc4425fcff40a43",  # pragma: allowlist secret
    "E28A": "a6bf1be2f16b7a5b4b16bbdef0e3194a6508915b0da99c6f50775683bd9378d1",  # pragma: allowlist secret
    "E28B": "54424f03415220b924ce109ab2fc5b373a3351c35f1bc8257cc101784bdc02da",  # pragma: allowlist secret
    "E28C": "0b049c788e14b09c31a804dd43632afdded3c2e08dadb6a230af3fe8133fcc6a",  # pragma: allowlist secret
    "E28D": "41994559852b393515da5769cc8255aa59a34e17ac7e8bba7999c53abd5220f4",  # pragma: allowlist secret
    "E28E": "6a678df411b3572e659848b229e03ac1a457ae2e03ad1bfe56be6a772917266e",  # pragma: allowlist secret
    "E28F": "1af5854365cf8ac3c5fcddbb1fcded958a25ca8c7eeb58c4135654bbbbaeb0ce",  # pragma: allowlist secret
    "E28G": "777569658ed80ef3770119389159665d30b1e538648a7ae8b2ed359c553ec1e1",  # pragma: allowlist secret
    "ITAP": "961d1fa9f24a9723908f5cc59491138428c957c2987fb57120fe4fec069e165a",  # pragma: allowlist secret
}


def test_all_eight_codes_are_projected() -> None:
    assert set(BY_CODE) == {"E28", "E28A", "E28B", "E28C", "E28D", "E28E", "E28F", "E28G"}


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["code"])
def test_row_obeys_existing_schema_and_preserves_source(row: dict) -> None:
    secondary = row["code"] in SECONDARY
    card = row["law_card"]
    assert row["family"] == "E28" and row["category"] == "KITAS/Limited Stay"
    assert row["permit_class"] == (None if secondary else "ITAS")
    assert row["entry_type"] == (None if secondary else "MULTIPLE")
    assert row["sponsor"] == (
        None if secondary else "REQUIRED" if row["code"] == "E28A" else "NOT_REQUIRED"
    )
    assert row["verification_status"] == ("TO_VERIFY" if secondary else "VERIFIED")
    assert row["official_name_id"] == card["official_name_id"]
    assert row["source_url"] == card["source_url"]
    assert row["source_url"].endswith("/" + row["code"])
    assert row["page_sha256"] == card["page_sha256"]
    for key in ("page_sha256", "card_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", row[key])
    assert row["retrieved_at"] == card["retrieved_at"]
    assert (
        row["card_sha256"]
        == hashlib.sha256((LAW_DIR / f"{row['code']}.json").read_bytes()).hexdigest()
    )
    for key in (
        "initial_stay_days",
        "max_total_days",
        "itap",
        "apply_channel",
        "valid_from",
        "valid_until",
        "superseded_by",
    ):
        assert row[key] is None


@pytest.mark.parametrize("code", sorted(SECONDARY))
def test_missing_cards_do_not_acquire_inferred_fields(code: str) -> None:
    row = BY_CODE[code]
    for key in (
        "permit_class",
        "is_sold",
        "sponsor",
        "entry_type",
        "extendable",
        "stay_options",
        "max_total_months",
    ):
        assert row[key] is None
    assert row["price_keys"] == []
    assert row["law_card"]["status"] == "NOT_FOUND"


def test_discrete_stays_and_cumulative_limits_are_separate() -> None:
    for code, row in BY_CODE.items():
        if code in SECONDARY:
            continue
        years = [1, 2] if code == "E28A" else [5, 10]
        assert row["stay_options"] == [{"value": y, "unit": "YEAR"} for y in years]
        assert row["max_total_months"] == (
            72 if code == "E28A" else None if code == "E28G" else 120
        )


def test_exact_prices_and_unknown_sales_status() -> None:
    assert BY_CODE["E28A"]["price_keys"] == PRICE_KEYS
    assert BY_CODE["E28A"]["is_sold"] is True
    prices = importer.load_prices()
    assert [prices[pk["category"]][pk["item_key"]]["price"] for pk in PRICE_KEYS] == [
        "19.000.000 IDR",
        "17.000.000 IDR",
        "18.000.000 IDR",
    ]
    for code, row in BY_CODE.items():
        if code != "E28A":
            assert row["is_sold"] is None and row["price_keys"] == []
    # A commercial permanent-stage product is not a verified per-code ITAP entitlement.
    assert "Investor KITAP + MERP" in prices["kitap_permits"]
    assert all(row["itap"] is None for row in ROWS)
    assert not any("PNBP" in pk["item_key"] for row in ROWS for pk in row["price_keys"])


def test_no_source_claim_is_lost_or_rewritten() -> None:
    assert set(SOURCE_CONTENT_SHA256) == {p.stem for p in LAW_DIR.glob("*.json")}
    for stem, expected in SOURCE_CONTENT_SHA256.items():
        card = json.loads((LAW_DIR / f"{stem}.json").read_text())
        canonical = json.dumps(
            card, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        assert hashlib.sha256(canonical).hexdigest() == expected, stem


def test_ambiguities_and_duration_scoped_alternatives_remain_visible() -> None:
    a = BY_CODE["E28A"]["law_card"]
    assert "1 tahun atau 2 tahun" in a["stay_days"][0]
    assert a["supplementary_stay_evidence"]["initial_stay_verbatim"] == "2 tahun"
    c = BY_CODE["E28C"]["law_card"]
    reqs = c["requirements_verbatim"]
    assert "US$350.000" in reqs[5]["text"] and "5 tahun" in reqs[5]["section"]
    assert "US$700.000; atau" in reqs[6]["text"] and "10 tahun" in reqs[6]["section"]
    assert "US$1.000.000" in reqs[7]["text"] and "10 tahun" in reqs[7]["section"]
    g = BY_CODE["E28G"]["law_card"]
    assert "current_classification_against_reported_absence" in g["to_verify"]
    assert BY_CODE["E28G"]["superseded_by"] is None


def test_migration_generated_cli_and_no_legacy_overwrite(tmp_path) -> None:
    sql = importer.emit_sql("E28")
    assert MIGRATION.read_text() == sql
    assert importer.main(["--family", "E28", "--check", str(MIGRATION)]) == 0
    drifted = tmp_path / "drift.sql"
    drifted.write_text(sql + "-- drift\n")
    assert importer.main(["--family", "E28", "--check", str(drifted)]) == 1
    guard = sql[sql.index("DO $guard$") : sql.index("$guard$;")]
    assert all(f"'{code}'" in guard for code in BY_CODE)
    assert sql.index("DO $guard$") < sql.index("INSERT INTO visa_types")
    assert "stay_options = EXCLUDED.stay_options" in sql
    rollback = sql.split("-- === ROLLBACK ===", 1)[1]
    assert "stay_options = NULL" in rollback
    assert "DELETE FROM" not in rollback
    for column in (
        "name",
        "category",
        "duration",
        "cost_visa",
        "requirements",
        "metadata",
        "last_updated",
    ):
        assert f"{column} = EXCLUDED" not in sql


@pytest.mark.parametrize(
    "family,filename",
    [("C", "331_visa_types_law_family_c.sql"), ("E30", "333_visa_types_law_family_e30.sql")],
)
def test_shared_importer_changes_leave_c_and_e30_byte_identical(family: str, filename: str) -> None:
    path = MIGRATIONS / filename
    assert path.read_bytes() == importer.emit_sql(family).encode()
    assert importer.main(["--family", family, "--check", str(path)]) == 0
