"""E33 projection, immutable extraction pins, exact prices and SQL regression checks."""

import hashlib
import json
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import pre_ruling_card

LAW_DIR = importer.HERE / "law" / "E33"
ROWS = {row["code"]: row for row in importer.build_rows("E33")}
MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "336_visa_types_law_family_e33.sql"
# Canonical JSON pins: the owner's group1 extraction plus the 2026-10-18 owner-ruling ITAP edits
# (cards' itap_* fields, ITAP.json assessments; everything else is unchanged from the extraction).
# These detect changes to any string, list item or source metadata despite formatting.
CONTENT_PINS = {
    "E33": "7adad865ea8080633b251373b6399bd0e3d4df951cbab44856509903d6755670",  # pragma: allowlist secret
    "E33A": "95efb852575ea77d276110da594c56a1a3170267489ea07129264e4060a3c734",  # pragma: allowlist secret
    "E33B": "e0b8f8af3d52ee04533bec0382196351d4e5bf0206053d4b5ea10d3d67f3ae2a",  # pragma: allowlist secret
    "E33C": "83c00193dfee782061342fcd3e91ca98076e95c755cfa441ab3a861418a0f3b6",  # pragma: allowlist secret
    "E33D": "a9bc60db42c1948f87e3701bfc794ca5e1b5cddf1a78d58d020dd1dd919d5dd9",  # pragma: allowlist secret
    "E33E": "5dfd73557675cf38ff767a69b0d0859dc0a9617a00052352ef0bbbab6bf976e1",  # pragma: allowlist secret
    "E33F": "8ab4c778804bf6e1a2fe16334e64e5b8a646fd5dd11641cf1bbda8400e49f226",  # pragma: allowlist secret
    "E33G": "331d2e6d1ecf25809abc61be9295b4700ecc74ce63a1e92e01e03490bf91b169",  # pragma: allowlist secret
    "ITAP": "d5fa1f8a12d7a1739c8554b5fdbb5103f03a7e0b1f2253e309092013d10b3412",  # pragma: allowlist secret
}
PRICE_KEYS = {
    "E33": ["E33 Second Home (5 Years)"],
    "E33A": [],
    "E33B": [],
    "E33C": [],
    "E33D": [],
    "E33E": ["E33E Second Home Senior (5 Years)", "E33E Second Home Senior (Extend)"],
    "E33F": [
        "E33F Second Home Senior (1 Year, Offshore)",
        "E33F Second Home Senior (1 Year, Altus/Onshore)",
        "E33F Second Home Senior (Extend)",
    ],
    "E33G": [
        "E33G Remote Worker (Altus/Onshore)",
        "E33G Remote Worker (Offshore)",
        "E33G Remote Worker (Extend)",
    ],
}


@pytest.mark.parametrize("code,digest", CONTENT_PINS.items())
def test_entire_card_and_itap_evidence_equal_canonical_extraction(code: str, digest: str) -> None:
    card = pre_ruling_card(json.loads((LAW_DIR / f"{code}.json").read_text()))
    canonical = json.dumps(card, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == digest


@pytest.mark.parametrize("code,row", ROWS.items())
def test_row_preserves_source_and_satisfies_existing_schema(code: str, row: dict) -> None:
    path = LAW_DIR / f"{code}.json"
    card = json.loads(path.read_text())
    assert row["family"] == "E33" and row["category"] == "KITAS/Limited Stay"
    assert row["law_card"] == card
    assert row["official_name_id"] == card["official_name_id"]
    assert row["source_url"] == card["source_url"]
    assert row["source_url"].endswith("/" + code)
    assert row["page_sha256"] == card["page_sha256"]
    assert row["card_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["retrieved_at"] == card["retrieved_at"]
    assert row["permit_class"] == (None if code == "E33D" else "ITAS")
    assert row["verification_status"] == ("TO_VERIFY" if code == "E33D" else "VERIFIED")
    assert row["initial_stay_days"] is None and row["max_total_days"] is None
    assert row["apply_channel"] is None and row["itap"] is None
    assert row["valid_from"] is None and row["valid_until"] is None
    assert row["superseded_by"] is None


def test_e33d_unknowns_are_not_filled_from_siblings() -> None:
    assert len(ROWS) == 8
    row = ROWS["E33D"]
    for key in (
        "permit_class",
        "entry_type",
        "sponsor",
        "is_sold",
        "extendable",
        "stay_options",
        "max_total_months",
    ):
        assert row[key] is None
    assert row["price_keys"] == []


def test_sponsors_distinguish_not_required_from_conditional() -> None:
    for code in ("E33", "E33B", "E33E", "E33G"):
        assert ROWS[code]["sponsor"] == "NOT_REQUIRED"
    for code in ("E33A", "E33C", "E33F"):
        assert ROWS[code]["sponsor"] == "REQUIRED"
    assert ROWS["E33D"]["sponsor"] is None
    # Existing C1/C2 semantics still mean CONDITIONAL, not NOT_REQUIRED.
    c = {r["code"]: r for r in importer.build_rows("C")}
    assert c["C1"]["sponsor"] == c["C2"]["sponsor"] == "CONDITIONAL"


def test_exact_commercial_keys_only_and_absence_does_not_mean_not_sold() -> None:
    prices = importer.load_prices()
    for code, keys in PRICE_KEYS.items():
        row = ROWS[code]
        assert row["price_keys"] == [{"category": "kitas_permits", "item_key": k} for k in keys]
        assert row["is_sold"] is (True if keys else None)
        for key in keys:
            assert key in prices["kitas_permits"]
            assert not prices["kitas_permits"][key].get("internal")
    assert all(row["itap"] is None for row in ROWS.values())


def test_year_options_and_cumulative_months_never_become_days() -> None:
    for code, years in {
        "E33": [5],
        "E33A": [5, 10],
        "E33B": [5, 10],
        "E33C": [5, 10],
        "E33E": [5],
        "E33F": [1],
        "E33G": [1],
    }.items():
        assert ROWS[code]["stay_options"] == [{"value": y, "unit": "YEAR"} for y in years]
        assert ROWS[code]["max_total_months"] == (72 if code in {"E33F", "E33G"} else 120)
        assert ROWS[code]["initial_stay_days"] is None


def test_generated_migration_guards_case_updates_law_only_and_rolls_back() -> None:
    sql = importer.emit_sql("E33")
    assert importer.latest_migration("E33") == "344_visa_types_law_rulings_2026_10_19.sql"
    forward, rollback = sql.split("-- === ROLLBACK ===", 1)
    assert forward.index("DO $guard$") < forward.index("INSERT INTO visa_types")
    guard = forward[forward.index("DO $guard$") : forward.index("$guard$;")]
    assert all(f"'{code}'" in guard for code in ROWS)
    assert "code <> upper(code)" in guard
    assert sql.count("INSERT INTO visa_types") == 8
    assert "stay_options = EXCLUDED.stay_options" in forward
    assert "stay_options = NULL" in rollback
    assert "verification_status = 'TO_VERIFY'" in rollback
    for legacy in (
        "name",
        "category",
        "description",
        "duration",
        "cost_visa",
        "requirements",
        "metadata",
    ):
        assert f"{legacy} = EXCLUDED" not in forward
    assert "CREATE TABLE" not in forward and "ALTER TABLE" not in forward
    assert "DELETE FROM" not in rollback and "DROP" not in rollback
    latest = MIGRATIONS / importer.latest_migration("E33")
    assert importer.main(["--family", "E33", "--check", str(latest)]) == 0


@pytest.mark.parametrize(
    "family,filename",
    [("C", "331_visa_types_law_family_c.sql"), ("E30", "333_visa_types_law_family_e30.sql")],
)
def test_previously_generated_migrations_remain_byte_identical(family: str, filename: str) -> None:
    assert importer.emit_sql(family).encode() == (MIGRATIONS / filename).read_bytes()
    assert importer.main(["--family", family, "--check", str(MIGRATIONS / filename)]) == 0


def test_bad_price_key_fails_before_projection() -> None:
    with pytest.raises(ValueError, match="not found in price list"):
        importer._check_price_keys(
            "E33",
            [{"category": "kitas_permits", "item_key": "E33 invented"}],
            importer.load_prices(),
        )
