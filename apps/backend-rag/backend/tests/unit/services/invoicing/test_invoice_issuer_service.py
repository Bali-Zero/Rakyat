"""Section-6 service-level spec tests for persisted invoice issuer binding.

Mocks email, Drive and the database: nothing is ever delivered or written.
Expected values come from synthetic entities and the spec, never from the
production registry contents.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, InvoiceEntity
from backend.services.invoicing.invoice_service import (
    _INVOICE_UPSERT_SQL,
    InvoiceAutomationService,
)

A_KEY = "cimb_bayu_bali_nol"
B_KEY = "mandiri_bali_nol_impresariat"
C_KEY = "bni_bayu_santero"
D_KEY = "permata_bali_nol_konsultan"

AWARE = datetime(2027, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
PRE_CUTOVER = datetime(2026, 11, 10, tzinfo=timezone.utc)
POST_CUTOVER = datetime(2027, 1, 2, tzinfo=timezone.utc)


def synthetic_entities() -> dict[str, InvoiceEntity]:
    base = {
        "tax_id": "00.000.000.0-000.000",
        "address": None,
        "bank_account_name": "Synthetic Holder One",
        "bank_account_number": "0000000001",
        "bank_name": "Synthetic Bank One",
        "bank_branch": "Synthetic Branch One",
        "bank_swift": "SYNTHIDJA",
    }
    a = InvoiceEntity(key=A_KEY, company_name="Synthetic Issuer One", **base)
    b = InvoiceEntity(
        key=B_KEY,
        company_name="Synthetic Issuer Two",
        **{**base, "tax_id": "11.111.111.1-111.111", "address": "Synthetic Street Two"},
    )
    c = InvoiceEntity(
        key=C_KEY,
        company_name="Synthetic Issuer Three",
        **{**base, "tax_id": "22.222.222.2-222.222", "bank_account_number": "0000000003"},
    )
    d = InvoiceEntity(
        key=D_KEY,
        company_name="Synthetic Issuer Four",
        **{
            **base,
            "tax_id": "33.333.333.3-333.333",
            "address": "Synthetic Street Four",
            "bank_account_number": "0000000004",
            "bank_branch": None,
            "bank_swift": None,
        },
    )
    return {A_KEY: a, B_KEY: b, C_KEY: c, D_KEY: d}


@pytest.fixture
def synth(monkeypatch: pytest.MonkeyPatch) -> dict[str, InvoiceEntity]:
    registry = synthetic_entities()
    monkeypatch.setattr(entities, "ENTITIES", registry)
    monkeypatch.setattr("backend.services.invoicing.invoice_service.ENTITIES", registry)
    return registry


@pytest.fixture
def db_pool():
    pool = MagicMock()
    conn = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool._conn = conn
    return pool


@pytest.fixture
def env(db_pool, monkeypatch):
    drive = MagicMock()
    drive.upload_file_to_folder = AsyncMock(
        return_value={"id": "drive-id", "webViewLink": "https://drive.example/x"}
    )
    gen = MagicMock()
    gen.generate_invoice_number = MagicMock(return_value="INV-202611-00001")
    gen.generate = MagicMock(return_value=b"%PDF-1.4 synthetic")
    email_client = MagicMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    email_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "backend.services.invoicing.invoice_service.get_email_client",
        AsyncMock(return_value=email_client),
    )
    monkeypatch.setattr(
        "backend.services.invoicing.invoice_service.log_email_attempt", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        "backend.services.invoicing.invoice_service.record_email_result", AsyncMock()
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(
        "backend.services.invoicing.invoice_service.invalidate_crm_stats", invalidate
    )
    with (
        patch(
            "backend.services.invoicing.invoice_service.ServiceAccountDriveService",
            return_value=drive,
        ),
        patch("backend.services.invoicing.invoice_service.InvoiceGenerator", return_value=gen),
    ):
        service = InvoiceAutomationService(db_pool)
    return service, invalidate, email_client


def practice_data(**overrides):
    data = {
        "id": 1,
        "client_id": 100,
        "practice_type_code": "kitas",
        "quoted_price": 5_000_000,
        "client_drive_folder_id": "folder-1",
    }
    return {**data, **overrides}


def client_data(**overrides):
    data = {"id": 100, "full_name": "Synthetic Client", "email": "client@example.test"}
    return {**data, **overrides}


def lookup_row(**overrides):
    row = {
        "db_now": AWARE,
        "practice_exists": True,
        "invoice_row_exists": False,
        "issuer_entity_key": None,
        "issuer_routed_at": None,
        "practice_documents": None,
    }
    return {**row, **overrides}


def stored_lookup(key, routed_at=None, db_now=AWARE):
    return lookup_row(
        db_now=db_now,
        invoice_row_exists=True,
        issuer_entity_key=key,
        issuer_routed_at=routed_at or AWARE,
    )


def binding_row(key, routed_at=None):
    return {"issuer_entity_key": key, "issuer_routed_at": routed_at or AWARE}


def practice_row():
    return {"client_id": 100, "quoted_price": 5_000_000}


def run_flow(service, conn, *, practice=None, lookup=(), persisted=None, with_update=True):
    side = [practice or practice_data(), client_data(), *lookup]
    if with_update:
        side += [practice_row(), persisted]
    conn.fetchrow = AsyncMock(side_effect=side)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return service.trigger_on_sending_invoice(1, "agent@example.test")


def upsert_call(conn):
    for call in conn.fetchrow.call_args_list:
        if call.args and call.args[0] is _INVOICE_UPSERT_SQL:
            return call.args
    raise AssertionError("guarded upsert was never executed")


def generator_entity(service):
    return service.invoice_generator.generate.call_args.kwargs.get("entity")


# ============================================================
# Entity wiring into the generator
# ============================================================


class TestGeneratorEntityWiring:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", [A_KEY, B_KEY, C_KEY], ids=["entity_a", "entity_b", "entity_c"])
    async def test_service_passes_resolved_entity_to_generator(self, env, synth, key):
        service, _, email_client = env
        conn = service.db_pool._conn
        result = await run_flow(
            service, conn, lookup=[stored_lookup(key)], persisted=binding_row(key)
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[key]
        args = upsert_call(conn)
        assert args[12] == key
        assert args[13] == AWARE
        assert args[14] == DEFAULT_ENTITY_KEY
        payload = email_client.post.call_args.kwargs["json"]
        assert (
            payload["attachments"][0]["content"] == base64.b64encode(b"%PDF-1.4 synthetic").decode()
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code", "key"),
        [
            ("kitas", A_KEY),
            ("visa_c1_tourism", B_KEY),
            ("tax_monthly_basic_0_50", D_KEY),
            ("ext_b1_voa", C_KEY),
        ],
    )
    async def test_new_invoice_routes_code_to_entity(self, env, synth, monkeypatch, code, key):
        monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code=code),
            lookup=[lookup_row(db_now=POST_CUTOVER)],
            persisted=binding_row(key, POST_CUTOVER),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[key]
        assert upsert_call(conn)[12] == key


# ============================================================
# Stored binding wins over current policy
# ============================================================


class TestStoredBindingWins:
    @pytest.mark.asyncio
    async def test_stored_issuer_wins_after_policy_change(self, env, synth, monkeypatch):
        monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", datetime(2026, 1, 1).date())
        service, _, _ = env
        conn = service.db_pool._conn
        stored_at = datetime(2026, 11, 20, tzinfo=timezone.utc)
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code="kitas"),
            lookup=[stored_lookup(B_KEY, stored_at, db_now=POST_CUTOVER)],
            persisted=binding_row(B_KEY, stored_at),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[B_KEY]
        args = upsert_call(conn)
        assert args[12] == B_KEY
        assert args[13] == stored_at, "stored routed_at must be preserved, not the lookup instant"


# ============================================================
# Legacy adoption
# ============================================================


class TestLegacyAdoption:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "db_now", [PRE_CUTOVER, POST_CUTOVER], ids=["before_cutoff", "after_cutoff"]
    )
    async def test_null_binding_adopts_default(self, env, synth, db_now):
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code="visa_c1_tourism"),
            lookup=[lookup_row(db_now=db_now, invoice_row_exists=True)],
            persisted=binding_row(A_KEY, db_now),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[A_KEY]
        assert upsert_call(conn)[12] == A_KEY

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "documents",
        [
            {"invoice": {"invoice_number": "INV-LEGACY-1"}},
            json.dumps({"invoice": {"invoice_number": "INV-LEGACY-1"}}),
            json.dumps(json.dumps({"invoice": {"invoice_number": "INV-LEGACY-1"}})),
            {"invoice": None},
            json.dumps({"invoice": None}),
        ],
        ids=["dict", "json_str", "double_encoded", "null_marker_dict", "null_marker_str"],
    )
    async def test_json_only_legacy_adopts_default(self, env, synth, documents):
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code="visa_c1_tourism"),
            lookup=[lookup_row(db_now=POST_CUTOVER, practice_documents=documents)],
            persisted=binding_row(A_KEY, POST_CUTOVER),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[A_KEY]
        assert upsert_call(conn)[12] == A_KEY


# ============================================================
# Retired codes keep supplied interim routing
# ============================================================


class TestRetiredCodes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code", "key"),
        [
            ("urgent_1_day", A_KEY),
            ("urgent_2_days", A_KEY),
            ("urgent_3_days", A_KEY),
            ("other_driving_license", B_KEY),
            ("other_create_molina_express", A_KEY),
        ],
    )
    async def test_retired_codes_route_as_stated(self, env, synth, monkeypatch, code, key):
        monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code=code),
            lookup=[lookup_row(db_now=POST_CUTOVER)],
            persisted=binding_row(key, POST_CUTOVER),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[key]

    @pytest.mark.asyncio
    async def test_create_molina_express_warns_once(self, env, caplog):
        service, _, _ = env
        conn = service.db_pool._conn
        with caplog.at_level(logging.WARNING, logger="backend.services.invoicing.entities"):
            result = await run_flow(
                service,
                conn,
                practice=practice_data(practice_type_code="other_create_molina_express"),
                lookup=[lookup_row(db_now=POST_CUTOVER)],
                persisted=binding_row(A_KEY, POST_CUTOVER),
            )
        assert result["success"] is True
        warnings = [
            r
            for r in caplog.records
            if r.name == "backend.services.invoicing.entities"
            and r.context.get("event") == "invoice_issuer_unmapped"  # type: ignore[attr-defined]
        ]
        assert len(warnings) == 1
