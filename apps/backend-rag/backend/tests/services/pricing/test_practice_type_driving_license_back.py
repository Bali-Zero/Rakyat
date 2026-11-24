"""Driving License is on sale again (owner ruling 2026-11-23, decision K135).

Guilt: on the previous main the row is missing from the sheet and the site
snapshot, the code is still in the retired review group, and migration 379 does
not exist.
Innocence: the issuer stays PT Bayu Bali Nol and the other four retired codes
stay retired.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.db.migration_base import split_migration_sql
from backend.services.invoicing.entities import ROUTING_REVIEW_GROUPS, entity_key_for_code

_BACKEND = Path(__file__).resolve().parents[3]
_CATALOGUE = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_SNAPSHOT = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"
_MIGRATION = _BACKEND / "db" / "migrations_v2" / "379_practice_types_driving_license_back.sql"

_CODE = "other_driving_license"
_KEY = "Driving License"
_ROW = {
    "name": "Driving License",
    "price": "1.600.000 IDR",
    "tier_range": None,
    "duration": "",
    "validity": "",
    "notes": "",
    "verified_on": "2026-11-23",
    "description_en": "Indonesian driving license (SIM) issuance for foreign nationals.",
    "icon_id": "other-driving",
}


def _other_process(path: Path, *parents: str) -> dict:
    node = json.loads(path.read_text(encoding="utf-8"))
    for parent in parents:
        node = node[parent]
    return node["other_process"]


def test_the_sheet_carries_exactly_the_ruled_row() -> None:
    assert _other_process(_CATALOGUE, "services")[_KEY] == _ROW


def test_the_site_snapshot_carries_the_same_price() -> None:
    row = _other_process(_SNAPSHOT, "services_by_category")[_KEY]
    assert row["price"] == "1.600.000 IDR"
    assert row["name"] == _ROW["name"]


def test_the_issuer_stays_pt_bayu_bali_nol() -> None:
    assert entity_key_for_code(_CODE) == "cimb_bayu_bali_nol"


def test_only_this_code_left_the_retired_group() -> None:
    assert ROUTING_REVIEW_GROUPS["retired"] == (
        "urgent_1_day",
        "urgent_2_days",
        "urgent_3_days",
        "other_create_molina_express",
    )


def test_migration_touches_only_this_row_and_can_be_rolled_back() -> None:
    forward, rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
    assert rollback is not None

    assert len(re.findall(r"^UPDATE practice_types", forward, re.MULTILINE)) == 1
    assert f"WHERE code = '{_CODE}';" in forward
    assert "is_active = true" in forward
    assert "base_price = 1600000" in forward
    assert "RAISE EXCEPTION" in forward

    assert len(re.findall(r"^UPDATE practice_types", rollback, re.MULTILINE)) == 1
    assert f"WHERE code = '{_CODE}';" in rollback
    assert "is_active = false" in rollback
    assert "base_price" not in rollback.split("UPDATE practice_types", 1)[1]
