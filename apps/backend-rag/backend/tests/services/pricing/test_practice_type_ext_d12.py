"""The D12 Business Investigation extension is a sold service (owner ruling
2026-11-23, decision K134).

Guilt: on the previous main the code has no issuer, the sheet and the site
snapshot have no row, and migration 380 does not exist.
Innocence: the two D12 issue rows keep their prices.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.db.migration_base import split_migration_sql
from backend.services.invoicing.entities import PRACTICE_TYPE_ENTITY_KEYS, entity_key_for_code

_BACKEND = Path(__file__).resolve().parents[3]
_CATALOGUE = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_SNAPSHOT = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"
_MIGRATION = _BACKEND / "db" / "migrations_v2" / "380_practice_types_ext_d12.sql"

_CODE = "ext_d12"
_KEY = "D12 Extension"
_ROW = {
    "name": "D12 Business Investigation — Extension",
    "price": "3.750.000 IDR",
    "tier_range": None,
    "duration": "180 days",
    "validity": "Extension only",
    "notes": "",
    "verified_on": "2026-11-23",
    "description_en": (
        "One extension of the D12 Business Investigation stay permit for a further 180 days."
    ),
    "icon_id": "visa-extension",
}


def _multiple_entry(path: Path, *parents: str) -> dict:
    node = json.loads(path.read_text(encoding="utf-8"))
    for parent in parents:
        node = node[parent]
    return node["multiple_entry_visas"]


def test_the_issuer_is_cv_bayu_santero() -> None:
    assert PRACTICE_TYPE_ENTITY_KEYS[_CODE] == "bni_bayu_santero"
    assert entity_key_for_code(_CODE) == "bni_bayu_santero"


def test_the_sheet_carries_exactly_the_ruled_row() -> None:
    assert _multiple_entry(_CATALOGUE, "services")[_KEY] == _ROW


def test_the_site_snapshot_carries_the_same_price() -> None:
    row = _multiple_entry(_SNAPSHOT, "services_by_category")[_KEY]
    assert row["price"] == "3.750.000 IDR"
    assert row["name"] == _ROW["name"]


def test_the_d12_issue_rows_keep_their_prices() -> None:
    rows = _multiple_entry(_CATALOGUE, "services")
    assert rows["D12 Business Investigation (1 Year)"]["price"] == "7.500.000 IDR"
    assert rows["D12 Business Investigation (2 Years)"]["price"] == "10.000.000 IDR"


def test_migration_adds_only_this_row_and_can_be_rolled_back() -> None:
    forward, rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
    assert rollback is not None

    assert len(re.findall(r"^INSERT INTO practice_types", forward, re.MULTILINE)) == 1
    assert f"'{_CODE}'" in forward
    assert "'D12 Business Investigation — Extension'" in forward
    assert "'visa_extension'" in forward
    assert "3750000" in forward
    assert "RAISE EXCEPTION" in forward

    assert f"WHERE code = '{_CODE}';" in rollback
    assert "is_active = false" in rollback
