"""The tax catalogue (owner ruling 2026-11-27, built 2026-11-28): nine monthly bands, the new tax
services, their bonus rates, delivery slots, invoice issuer and public price list rows.

The DB side (migration 398 on a migrated schema) is tests/db/test_tax_catalogue_398_pg.py; the
amounts below are the independent oracle both files read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.db.migration_base import split_migration_sql
from backend.services.crm.delivery_catalog import _catalog, delivery_policy
from backend.services.invoicing.entities import PRACTICE_TYPE_ENTITY_KEYS, entity_key_for_code

_BACKEND = Path(__file__).resolve().parents[3]
_CATALOGUE = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_SNAPSHOT = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"
MIGRATION = _BACKEND / "db" / "migrations_v2" / "398_tax_catalogue_bands_services_rates.sql"

BANDS = (
    "0_50",
    "50_100",
    "100_150",
    "150_200",
    "200_250",
    "250_300",
    "300_350",
    "350_400",
    "400_plus",
)
BAND_BONUS = (150000, 250000, 350000, 450000, 550000, 650000, 750000, 850000, 950000)
NEW_BANDS = BANDS[2:]
WIDE_CODES = (
    "tax_monthly_basic_100_200",
    "tax_monthly_basic_200_plus",
    "tax_monthly_bundled_100_200",
    "tax_monthly_bundled_200_plus",
)
#: Where each new band takes its client price from: the list band that contains it.
SOURCE_OF_NEW_BAND = {
    "100_150": "100_200",
    "150_200": "100_200",
    "200_250": "200_plus",
    "250_300": "200_plus",
    "300_350": "200_plus",
    "350_400": "200_plus",
    "400_plus": "200_plus",
}
EXISTING_SERVICE_BONUS = {
    "tax_annual_personal": 150000,
    "tax_annual_personal_addl": 300000,
    "tax_annual_company": 1000000,
    "tax_annual_reporting": 500000,
    "tax_annual_pkg_a": 1000000,
    "tax_annual_pkg_b": 1000000,
    "tax_annual_pkg_c": 1000000,
    "tax_annual_pkg_d": 1000000,
    "tax_lkpm_yearly": 100000,
    "tax_npwp_personal_coretax": 125000,
}
#: code -> (name, base_price or None when included in the monthly price, bonus)
NEW_SERVICES = {
    "tax_npwp_personal_no_kitas": (
        "NPWP Personal + Coretax Activation (without KITAS)",
        1500000,
        100000,
    ),
    "tax_sertel": ("Electronic Certificate (SERTEL)", 1000000, 100000),
    "tax_coretax_activation_personal": ("Coretax Activation — Personal", 1000000, 100000),
    "tax_coretax_activation_company": ("Coretax Activation — Company", 1000000, 100000),
    "tax_kpp_accompaniment": ("Tax Office (KPP) Accompaniment", 1000000, 150000),
    "tax_abt_registration": ("Groundwater Tax (ABT) Registration", 2500000, 500000),
    "tax_meeting": ("Tax Meeting", 1000000, 200000),
    "tax_monthly_billing_bpjs_tk": ("Monthly Billing — BPJS Ketenagakerjaan", None, 0),
    "tax_monthly_billing_bpjs_ks": ("Monthly Billing — BPJS Kesehatan", None, 0),
    "tax_monthly_billing_npwpd": ("Monthly Billing — NPWPD", None, 0),
}
NEW_SLOTS = {
    "tax_npwp_personal_no_kitas": [
        "Personal NPWP",
        "Coretax activation confirmation \u00b7 redacted",
    ],
    "tax_sertel": ["Electronic certificate (SERTEL) issuance confirmation \u00b7 masked"],
    "tax_coretax_activation_personal": ["Coretax activation confirmation \u00b7 redacted"],
    "tax_coretax_activation_company": ["Coretax activation confirmation \u00b7 redacted"],
    "tax_kpp_accompaniment": ["Tax office visit record or receipt"],
    "tax_abt_registration": ["Issued ABT registration for [registered site]"],
    "tax_meeting": ["Meeting summary"],
    "tax_monthly_billing_bpjs_tk": ["BPJS Ketenagakerjaan payment receipt \u00b7 per month billed"],
    "tax_monthly_billing_bpjs_ks": ["BPJS Kesehatan payment receipt \u00b7 per month billed"],
    "tax_monthly_billing_npwpd": ["Regional tax payment receipt \u00b7 per month billed"],
}


def _band_codes(bands=BANDS) -> list[str]:
    return [f"tax_monthly_{family}_{band}" for family in ("basic", "bundled") for band in bands]


def expected_rates() -> dict[str, int]:
    rates = {
        code: bonus
        for family in ("basic", "bundled")
        for band, bonus in zip(BANDS, BAND_BONUS, strict=True)
        for code in [f"tax_monthly_{family}_{band}"]
    }
    rates.update(EXISTING_SERVICE_BONUS)
    rates.update({code: bonus for code, (_n, _p, bonus) in NEW_SERVICES.items()})
    return rates


def test_there_are_38_rates_and_none_for_registration_or_the_wide_codes() -> None:
    rates = expected_rates()
    assert len(rates) == 38
    assert "tax_registration" not in rates
    assert not set(WIDE_CODES) & set(rates)
    # Basic and bundled earn the same amount in the same band.
    for band in BANDS:
        assert rates[f"tax_monthly_basic_{band}"] == rates[f"tax_monthly_bundled_{band}"]


def test_the_migration_creates_exactly_the_ruled_rows_and_rates() -> None:
    forward, rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    assert rollback, "the migration must carry its rollback"
    for code, amount in expected_rates().items():
        assert (
            f"('{code}', {amount}, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue')"
            in forward
        ), code
    assert forward.count("'2026-11-01', TRUE") == 38
    assert "('tax_registration'" not in forward.split("-- Postcondition")[0]
    for code in _band_codes(NEW_BANDS):
        assert f"('{code}'," in forward, code
    for code, (name, price, _bonus) in NEW_SERVICES.items():
        # The whole tuple of THIS service, not a match anywhere in the file.
        tuple_re = (
            rf"\('{re.escape(code)}', '{re.escape(name)}',\s+'[^']*',\s+"
            rf"'tax', {price if price is not None else 'NULL'}, NULL, true\)"
        )
        assert re.search(tuple_re, forward), code


def test_every_new_band_copies_the_price_of_its_list_band_in_the_migration() -> None:
    forward, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    for family in ("basic", "bundled"):
        for band, source in SOURCE_OF_NEW_BAND.items():
            # The source code is the LAST element of this band's own VALUES tuple.
            tuple_re = rf"\('tax_monthly_{family}_{band}', '[^']*',\s+'[^']*',\s+'tax_monthly_{family}_{source}'\)"
            assert re.search(tuple_re, forward), (family, band)


def test_the_delivery_catalogue_has_every_new_code_and_keeps_the_wide_ones() -> None:
    catalog = _catalog()
    for code in _band_codes(NEW_BANDS) + list(NEW_SERVICES):
        policy = delivery_policy(code)
        assert policy is not None, code
        assert policy["bonus_trigger"] == "service_delivered", code
        assert policy["bonus_slot"] is None and policy["followup_slot"] is None, code
    for code in WIDE_CODES:
        assert code in catalog, "a practice already on a wide code still needs its slots"
    for family in ("basic", "bundled"):
        for band, source in SOURCE_OF_NEW_BAND.items():
            assert (
                delivery_policy(f"tax_monthly_{family}_{band}")["slots"]
                == delivery_policy(f"tax_monthly_{family}_{source}")["slots"]
            ), (family, band)
    # The bundled family keeps its LKPM/OSS/Annual slots, the basic one does not.
    assert len(delivery_policy("tax_monthly_bundled_400_plus")["slots"]) == 6
    assert len(delivery_policy("tax_monthly_basic_400_plus")["slots"]) == 2
    for code, labels in NEW_SLOTS.items():
        assert [s["label"] for s in delivery_policy(code)["slots"]] == labels, code
    # Redaction and per-month wording are mandatory qualifiers kept in the label.
    assert delivery_policy("tax_sertel")["slots"][0]["label"].endswith("· masked")
    assert delivery_policy("tax_monthly_billing_npwpd")["slots"][0]["label"].endswith(
        "· per month billed"
    )


def test_the_renamed_npwp_service_and_the_new_one_have_the_ruled_names_in_the_catalogue() -> None:
    import csv

    rows = {
        r["service_code"]: r["service"]
        for r in csv.DictReader(
            (_BACKEND / "services/crm/delivery_catalog.csv").open(encoding="utf-8", newline="")
        )
    }
    assert rows["tax_npwp_personal_coretax"] == "NPWP Personal + Coretax Activation (with KITAS)"
    assert rows["tax_npwp_personal_no_kitas"] == NEW_SERVICES["tax_npwp_personal_no_kitas"][0]


def test_every_new_tax_code_is_invoiced_by_the_tax_company() -> None:
    golden = json.loads(
        (_BACKEND / "tests/fixtures/invoicing/invoice_code_entity_golden.json").read_text(
            encoding="utf-8"
        )
    )
    for code in _band_codes(NEW_BANDS) + list(NEW_SERVICES):
        assert PRACTICE_TYPE_ENTITY_KEYS[code] == "permata_bali_nol_konsultan", code
        assert golden[code] == "permata_bali_nol_konsultan", code
        assert entity_key_for_code(code) == "permata_bali_nol_konsultan", code
    # The wide codes keep their issuer: practices on them still invoice.
    for code in WIDE_CODES:
        assert PRACTICE_TYPE_ENTITY_KEYS[code] == "permata_bali_nol_konsultan", code


def _block(doc: dict) -> dict:
    return doc["services"]["tax_accounting"]["standalone_services"]


def test_the_public_list_carries_the_priced_new_services_and_not_the_included_ones() -> None:
    doc = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    block = _block(doc)
    priced = {n: p for _c, (n, p, _b) in NEW_SERVICES.items() if p is not None}
    assert set(block) == set(priced)
    for name, price in priced.items():
        row = block[name]
        assert row["name"] == name
        assert row["price"] == f"{price:,}".replace(",", ".") + " IDR"
        assert row["description_en"], name
    # "Included in the monthly price" has no price of its own, so it has no row: a row without a price
    # would read as "Contact for quote" on the site.
    flat = json.dumps(doc, ensure_ascii=False)
    assert "Monthly Billing" not in flat
    # The four public monthly price bands are unchanged.
    for family in ("monthly_tax_basic", "monthly_tax_bundled"):
        assert list(doc["services"]["tax_accounting"][family]) == [
            "Tier 0-50",
            "Tier 50-100",
            "Tier 100-200",
            "Tier 200+",
        ]


def test_the_site_snapshot_mirrors_the_new_block() -> None:
    doc = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["services_by_category"]
    mirror = snap["tax_accounting.standalone_services"]
    assert list(mirror) == list(_block(doc))
    for key, row in _block(doc).items():
        assert mirror[key]["price"] == row["price"] and mirror[key]["name"] == row["name"]
