"""Migration 398 against the readers: the release order of the tax catalogue.

`release_command` applies a migration while OLD machines still serve. A code that 398 inserts or
activates must therefore already be known to the invoice issuer routing and to the delivery catalogue
on the code that is live BEFORE the migration lands (reproduced on 2026-11-28: an unknown tax code is
invoiced by the default issuer, and the issuer is stored on the invoice; a code with no delivery
policy answers 409 on delivery). The readers ship first; this file fails if 398 names a code they
do not know.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.db.migration_base import split_migration_sql
from backend.services.crm.delivery_catalog import delivery_policy
from backend.services.invoicing.entities import PRACTICE_TYPE_ENTITY_KEYS
from backend.tests.services.pricing.test_tax_catalogue_2026_11_28 import (
    NEW_BANDS,
    NEW_SERVICES,
    SOURCE_OF_NEW_BAND,
    _band_codes,
    expected_rates,
)

_BACKEND = Path(__file__).resolve().parents[3]
MIGRATION = _BACKEND / "db" / "migrations_v2" / "398_tax_catalogue_bands_services_rates.sql"
_GOLDEN = _BACKEND / "tests" / "fixtures" / "invoicing" / "invoice_code_entity_golden.json"


def _inserted_codes() -> set[str]:
    """Every practice type 398 inserts: the head of each VALUES tuple before the rates section."""
    forward, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    section = forward.split("-- 4. Bonus rates.")[0]
    return set(re.findall(r"^\s+\('(tax_[a-z0-9_]+)', '", section, re.M))


def _activating_updates() -> list[str]:
    """UPDATE statements of the forward SQL that switch an EXISTING practice type on."""
    forward, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    return re.findall(r"UPDATE practice_types\s+SET[^;]*is_active\s*=\s*true[^;]*;", forward, re.I)


def test_the_codes_398_inserts_are_exactly_the_24_the_oracle_names() -> None:
    assert _inserted_codes() == set(_band_codes(NEW_BANDS)) | set(NEW_SERVICES)
    # 398 activates nothing that already exists: its only activations are on the rows it inserts.
    assert not _activating_updates()


def test_every_code_398_inserts_is_known_to_the_readers_that_ship_first() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    for code in sorted(_inserted_codes()):
        assert PRACTICE_TYPE_ENTITY_KEYS.get(code) == "permata_bali_nol_konsultan", (
            f"{code}: no tax issuer in entities.py, the old code would invoice it by default"
        )
        assert golden.get(code) == "permata_bali_nol_konsultan", f"{code}: not in the golden"
        assert delivery_policy(code) is not None, f"{code}: no delivery policy, delivery gives 409"


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
