"""Hospitality & F&B operational licences (owner ruling 2026-11-27).

Pins:
  1. Official PricingTool catalogue rows for SLHS (10M), SLS Villa (6M) and
     SLS Hotel (12M), plus innocence on Alcohol License (4M / 10M / 12M),
     Halal Certificate and PIRT.
  2. Mouth snapshot parity on all six hospitality rows.
  3. Invoice entity routing of the six CRM practice_types codes to
     PT Bali Nol Impresariat (mandiri_bali_nol_impresariat).
  4. Migration 389 forward/rollback/postconditions and exact name parity with
     the PricingTool catalogue.
  5. Automatic seeding of practice_required_documents from
     practice_types.required_documents on create and service switch.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.routers.crm_practices import (
    _normalize_required_doc_slot,
    _seed_practice_required_documents,
)
from backend.db.migration_base import split_migration_sql
from backend.services.invoicing.entities import PRACTICE_TYPE_ENTITY_KEYS, entity_key_for_code

_BACKEND = Path(__file__).resolve().parents[3]
_CATALOGUE = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_SNAPSHOT = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"
_MIGRATION = _BACKEND / "db" / "migrations_v2" / "389_practice_types_hospitality_licences.sql"

_RULED_ROWS = {
    "SLHS (Sertifikat Laik Higiene Sanitasi)": {
        "name": "SLHS (Sertifikat Laik Higiene Sanitasi)",
        "price": "10.000.000 IDR",
        "tier_range": None,
        "duration": "3-4 weeks",
        "validity": "",
        "notes": (
            "Government filing fee (Rp 0) included; third-party KAN lab testing, "
            "PKP food-safety training and staff medical checks are pass-through costs."
        ),
        "verified_on": "2026-11-27",
        "description_en": (
            "Food hygiene and sanitation certificate (SLHS) for restaurants, "
            "cafés and other food and beverage businesses."
        ),
        "icon_id": "other-skck",
    },
    "SLS — Villa & Boutique Accommodation": {
        "name": "SLS — Villa & Boutique Accommodation",
        "price": "6.000.000 IDR",
        "tier_range": None,
        "duration": "2-4 weeks",
        "validity": "",
        "notes": (
            "For standalone villas and boutique stays without a commercial "
            "restaurant (up to 5-6 rooms). Third-party KAN water lab testing "
            "is a pass-through cost."
        ),
        "verified_on": "2026-11-27",
        "description_en": (
            "Healthworthiness certificate (Sertifikat Laik Sehat) for standalone "
            "villas and boutique tourist accommodations without a commercial restaurant."
        ),
        "icon_id": "other-skck",
    },
    "SLS — Hotel & Resort": {
        "name": "SLS — Hotel & Resort",
        "price": "12.000.000 IDR",
        "tier_range": None,
        "duration": "3-6 weeks",
        "validity": "",
        "notes": (
            "For hotels, resorts and accommodations with a commercial kitchen or "
            "restaurant. Third-party KAN lab testing and staff health checks are "
            "pass-through costs."
        ),
        "verified_on": "2026-11-27",
        "description_en": (
            "Healthworthiness certificate (Sertifikat Laik Sehat) for hotels, "
            "resorts and tourist accommodations operating a commercial kitchen or restaurant."
        ),
        "icon_id": "other-skck",
    },
}

_PRACTICE_TYPE_SPECS = {
    "company_slhs": ("SLHS (Sertifikat Laik Higiene Sanitasi)", "10000000"),
    "company_sls_villa": ("SLS — Villa & Boutique Accommodation", "6000000"),
    "company_sls_hotel": ("SLS — Hotel & Resort", "12000000"),
    "company_alcohol_a": ("Alcohol License — Golongan A", "4000000"),
    "company_alcohol_bc": ("Alcohol License — Golongan B + C", "10000000"),
    "company_alcohol_abc": ("Alcohol License — Golongan A + B + C", "12000000"),
}


def _company_services(path: Path, *parents: str) -> dict:
    node = json.loads(path.read_text(encoding="utf-8"))
    for parent in parents:
        node = node[parent]
    return node["company_services"]


@pytest.mark.parametrize(("key", "expected"), sorted(_RULED_ROWS.items()))
def test_catalogue_carries_exact_ruled_hospitality_rows(key: str, expected: dict) -> None:
    assert _company_services(_CATALOGUE, "services")[key] == expected


def test_innocence_neighbouring_company_licences_keep_their_prices() -> None:
    rows = _company_services(_CATALOGUE, "services")
    assert rows["New Company (PT PMA)"]["price"] == "25.000.000 IDR"
    assert rows["Alcohol License — Golongan A"]["price"] == "4.000.000 IDR"
    assert rows["Alcohol License — Golongan B + C"]["price"] == "10.000.000 IDR"
    assert rows["Alcohol License — Golongan A + B + C"]["price"] == "12.000.000 IDR"
    assert rows["Halal Certificate"]["price"] == "Depend (Contact for quote)"
    assert rows["PIRT (Home Industry Food)"]["price"] == "Depend (Contact for quote)"


def test_mouth_snapshot_matches_all_six_hospitality_rows() -> None:
    cat_rows = _company_services(_CATALOGUE, "services")
    snap_rows = _company_services(_SNAPSHOT, "services_by_category")
    for _code, (label, _idr) in _PRACTICE_TYPE_SPECS.items():
        assert snap_rows[label]["price"] == cat_rows[label]["price"]
        assert snap_rows[label]["name"] == cat_rows[label]["name"]


@pytest.mark.parametrize("code", sorted(_PRACTICE_TYPE_SPECS))
def test_all_six_codes_route_to_bali_nol_impresariat(code: str) -> None:
    assert PRACTICE_TYPE_ENTITY_KEYS[code] == "mandiri_bali_nol_impresariat"
    assert entity_key_for_code(code) == "mandiri_bali_nol_impresariat"


def test_migration_389_inserts_six_reconciled_skus_with_checklists_and_rollback() -> None:
    forward, rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
    assert rollback is not None

    assert len(re.findall(r"^INSERT INTO practice_types", forward, re.MULTILINE)) == 1
    assert "ON CONFLICT (code) DO UPDATE" in forward
    assert "RAISE EXCEPTION" in forward

    cat_rows = _company_services(_CATALOGUE, "services")
    for code, (label, raw_idr) in _PRACTICE_TYPE_SPECS.items():
        assert f"'{code}'" in forward
        assert f"'{label}'" in forward
        assert raw_idr in forward
        # Exact name parity guarantees test_price_store_reconciliation.py joins cleanly
        assert cat_rows[label]["name"] == label
        assert f"'{code}'" in rollback

    assert "is_active = false" in rollback


def test_normalize_required_doc_slot_respects_column_widths_and_legacy_slugs() -> None:
    assert _normalize_required_doc_slot("bank_statement") == ("bank_statement", "Bank Statement")
    assert _normalize_required_doc_slot("Akta Pendirian & SK Kemenkumham") == (
        "akta_pendirian_sk_kemenkumham",
        "Akta Pendirian & SK Kemenkumham",
    )
    assert _normalize_required_doc_slot("   ") is None
    long_item = "A" * 120
    slug, label = _normalize_required_doc_slot(long_item) or ("", "")
    assert len(slug) == 50
    assert len(label) == 100


@pytest.mark.asyncio
async def test_seed_practice_required_documents_inserts_deduplicated_slots() -> None:
    conn = AsyncMock()
    count = await _seed_practice_required_documents(
        conn,
        practice_id=777,
        required_documents=[
            "Akta Pendirian & SK Kemenkumham",
            "Company NPWP & NIB (KBLI 56xxx)",
            "Akta Pendirian & SK Kemenkumham",  # duplicate slug ignored
            "",
        ],
        created_by="krisna@balizero.com",
    )
    assert count == 2
    conn.execute.assert_awaited_once()
    sql, practice_id, doc_types, doc_labels, author = conn.execute.await_args.args
    assert "INSERT INTO practice_required_documents" in sql
    assert "ON CONFLICT (practice_id, document_type) DO NOTHING" in sql
    assert practice_id == 777
    assert doc_types == [
        "akta_pendirian_sk_kemenkumham",
        "company_npwp_nib_kbli_56xxx",
    ]
    assert doc_labels == [
        "Akta Pendirian & SK Kemenkumham",
        "Company NPWP & NIB (KBLI 56xxx)",
    ]
    assert author == "krisna@balizero.com"


@pytest.mark.asyncio
async def test_seed_practice_required_documents_noops_when_empty() -> None:
    conn = AsyncMock()
    assert await _seed_practice_required_documents(conn, 1, None, "system") == 0
    assert await _seed_practice_required_documents(conn, 1, [], "system") == 0
    conn.execute.assert_not_awaited()


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_create_practice_seeds_required_documents_from_practice_type() -> None:
    from backend.app.routers.crm_practices import PracticeCreate, create_practice

    pool, conn = _mock_pool()
    type_row = {
        "id": 91,
        "base_price": Decimal("10000000"),
        "category": "company",
        "name": "SLHS (Sertifikat Laik Higiene Sanitasi)",
        "required_documents": [
            "Akta Pendirian & SK Kemenkumham",
            "Company NPWP & NIB (KBLI 56xxx)",
        ],
    }
    inserted_row = {
        "id": 501,
        "uuid": "00000000-0000-0000-0000-000000000501",
        "client_id": 42,
        "practice_type_id": 91,
        "status": "inquiry",
        "priority": "normal",
        "quoted_price": Decimal("10000000"),
        "actual_price": None,
        "payment_status": "unpaid",
        "assigned_to": "krisna@balizero.com",
        "start_date": None,
        "completion_date": None,
        "expiry_date": None,
        "created_at": datetime(2026, 11, 27, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 11, 27, tzinfo=timezone.utc),
        "client_visible": True,
    }
    conn.fetchrow = AsyncMock(
        side_effect=[type_row, {"assigned_to": "krisna@balizero.com"}, inserted_row]
    )
    conn.execute = AsyncMock(return_value=None)

    with (
        patch("backend.app.routers.crm_practices.verify_client_access", new=AsyncMock()),
        patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
        patch("backend.app.routers.crm_practices.log_database_operation"),
        patch("backend.app.routers.crm_practices.log_success"),
    ):
        await create_practice(
            request=MagicMock(),
            practice=PracticeCreate(client_id=42, practice_type_code="company_slhs"),
            background_tasks=MagicMock(),
            db_pool=pool,
            current_user={"email": "krisna@balizero.com", "role": "admin"},
        )

    seed_calls = [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO practice_required_documents" in call.args[0]
    ]
    assert len(seed_calls) == 1
    assert seed_calls[0].args[1] == 501
    assert seed_calls[0].args[2] == [
        "akta_pendirian_sk_kemenkumham",
        "company_npwp_nib_kbli_56xxx",
    ]


@pytest.mark.asyncio
async def test_update_practice_seeds_required_documents_on_service_selection() -> None:
    from backend.app.routers.crm_practices import PracticeUpdate, update_practice

    pool, conn = _mock_pool()
    old_row = {
        "status": "inquiry",
        "client_id": 42,
        "client_visible": True,
        "created_by": "zero@balizero.com",
        "assigned_to": "krisna@balizero.com",
        "practice_type_code": "open_inquiry",
    }
    new_type_row = {
        "id": 92,
        "base_price": Decimal("6000000"),
        "required_documents": [
            "Villa Lease/Title Deed & PBG/SLF",
            "Clean Water & Pool KAN Lab Results",
        ],
    }
    updated_row = {
        "id": 502,
        "status": "waiting_documents",
        "client_visible": True,
        "practice_type_id": 92,
        "client_id": 42,
        "practice_type_code": "company_sls_villa",
        "practice_type_name": "SLS — Villa & Boutique Accommodation",
        "completion_date": None,
    }
    conn.fetchrow = AsyncMock(side_effect=[old_row, new_type_row, updated_row])
    conn.fetchval = AsyncMock(side_effect=[502, False])
    conn.execute = AsyncMock(return_value=None)

    with (
        patch("backend.app.routers.crm_practices.is_crm_admin", return_value=True),
        patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
        patch("backend.app.routers.crm_practices.to_jsonb", return_value="{}"),
    ):
        await update_practice(
            request=MagicMock(),
            practice_id=502,
            updates=PracticeUpdate(
                status="waiting_documents",
                practice_type_code="company_sls_villa",
            ),
            db_pool=pool,
            current_user={"email": "zero@balizero.com", "role": "admin"},
        )

    seed_calls = [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO practice_required_documents" in call.args[0]
    ]
    assert len(seed_calls) == 1
    assert seed_calls[0].args[1] == 502
    assert seed_calls[0].args[2] == [
        "villa_lease_title_deed_pbg_slf",
        "clean_water_pool_kan_lab_results",
    ]

