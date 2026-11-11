"""Section-6 service-level failure-path tests: metadata blocks and resolution.

Companion to test_invoice_issuer_service.py. Email, Drive and the database
are fully mocked: nothing is rendered, sent, uploaded or written.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import InvoiceEntity
from backend.services.invoicing.invoice_service import InvoiceAutomationService

A_KEY = "cimb_bayu_bali_nol"
B_KEY = "mandiri_bali_nol_impresariat"

AWARE = datetime(2027, 1, 2, 0, 0, 0, tzinfo=timezone.utc)


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
    return {A_KEY: a, B_KEY: b}


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


def run_flow(service, conn, *, practice=None, lookup=()):
    side = [practice or practice_data(), client_data(), *lookup]
    conn.fetchrow = AsyncMock(side_effect=side)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return service.trigger_on_sending_invoice(1, "agent@example.test")


def assert_no_writes(service, conn, invalidate):
    assert service.invoice_generator.generate.call_count == 0
    assert service.drive_service.upload_file_to_folder.call_count == 0
    assert conn.execute.call_count == 0
    invalidate.assert_not_awaited()


class _SqlStateError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(f"synthetic pg error {sqlstate}")
        self.sqlstate = sqlstate


# ============================================================
# Orphaned / ambiguous metadata blocks issuance
# ============================================================


class TestMetadataBlocks:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("documents", "reason"),
        [
            ({"invoice": {"issuer_entity_key": "bni_bayu_santero"}}, "orphaned_issuer_metadata"),
            ({"invoice": {"issuer_routed_at": AWARE.isoformat()}}, "orphaned_issuer_metadata"),
            ("{not valid json", "ambiguous_legacy_metadata"),
            (json.dumps("{still not an object"), "ambiguous_legacy_metadata"),
            (json.dumps([1, 2, 3]), "ambiguous_legacy_metadata"),
            ({"invoice": "a string marker"}, "ambiguous_legacy_metadata"),
        ],
        ids=["orphaned_key", "orphaned_routed_at", "malformed", "double_malformed", "list", "str"],
    )
    async def test_orphaned_or_ambiguous_metadata_blocks(self, env, documents, reason, caplog):
        service, invalidate, email_client = env
        conn = service.db_pool._conn
        with caplog.at_level(logging.ERROR):
            result = await run_flow(
                service, conn, lookup=[lookup_row(practice_documents=documents)]
            )
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_resolution_failed"
        assert reason in result["error"]
        assert_no_writes(service, conn, invalidate)
        email_client.post.assert_not_awaited()
        errors = [
            r
            for r in caplog.records
            if r.name == "backend.services.invoicing.invoice_service"
            and r.context.get("event") == "invoice_issuer_resolution_failed"  # type: ignore[attr-defined]
        ]
        assert len(errors) == 1
        assert errors[0].context["reason"] == reason  # type: ignore[attr-defined]


# ============================================================
# Resolution failure has no side effects
# ============================================================


def _resolution_cases():
    return [
        ("lookup_failed", "lookup_failed", lambda: RuntimeError("db unreachable"), {}),
        ("issuer_schema_missing", "issuer_schema_missing", lambda: _SqlStateError("42703"), {}),
        ("practice_missing", "practice_missing", None, {"practice_exists": False}),
        ("invalid_lookup_result", "invalid_lookup_result", None, {"invoice_row_exists": "yes"}),
        (
            "invalid_routing_timestamp",
            "invalid_routing_timestamp",
            None,
            {"db_now": datetime(2027, 1, 2)},
        ),
        (
            "unknown_stored_issuer",
            "unknown_stored_issuer",
            None,
            {
                "invoice_row_exists": True,
                "issuer_entity_key": "synthetic_unknown",
                "issuer_routed_at": AWARE,
            },
        ),
        (
            "blank_key",
            "invalid_stored_binding",
            None,
            {"invoice_row_exists": True, "issuer_entity_key": "   ", "issuer_routed_at": AWARE},
        ),
        (
            "key_only",
            "invalid_stored_binding",
            None,
            {"invoice_row_exists": True, "issuer_entity_key": B_KEY},
        ),
        (
            "naive_stored_routed_at",
            "invalid_stored_binding",
            None,
            {
                "invoice_row_exists": True,
                "issuer_entity_key": B_KEY,
                "issuer_routed_at": datetime(2027, 1, 2),
            },
        ),
        (
            "str_stored_routed_at",
            "invalid_stored_binding",
            None,
            {
                "invoice_row_exists": True,
                "issuer_entity_key": B_KEY,
                "issuer_routed_at": "2027-01-02",
            },
        ),
    ]


class TestResolutionFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", _resolution_cases(), ids=[c[0] for c in _resolution_cases()])
    async def test_resolution_failure_has_no_side_effects(self, env, case, caplog):
        _case_id, reason, exc_factory, overrides = case
        service, invalidate, email_client = env
        conn = service.db_pool._conn
        lookup = [exc_factory()] if exc_factory else [lookup_row(**overrides)]
        with caplog.at_level(logging.ERROR):
            result = await run_flow(service, conn, lookup=lookup)
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_resolution_failed"
        assert reason in result["error"]
        assert_no_writes(service, conn, invalidate)
        email_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_practice_type_code_fails(self, env, caplog):
        service, invalidate, _ = env
        conn = service.db_pool._conn
        with caplog.at_level(logging.ERROR):
            result = await run_flow(
                service,
                conn,
                practice=practice_data(practice_type_code=123),
                lookup=[lookup_row()],
            )
        assert result["error_code"] == "invoice_issuer_resolution_failed"
        assert "invalid_practice_type_code" in result["error"]
        assert_no_writes(service, conn, invalidate)

    @pytest.mark.asyncio
    async def test_incomplete_stored_issuer_fails(self, env, monkeypatch, caplog):
        registry = synthetic_entities()
        registry[B_KEY] = InvoiceEntity(**{**registry[B_KEY].__dict__, "bank_name": ""})
        monkeypatch.setattr(entities, "ENTITIES", registry)
        monkeypatch.setattr("backend.services.invoicing.invoice_service.ENTITIES", registry)
        service, invalidate, _ = env
        conn = service.db_pool._conn
        with caplog.at_level(logging.ERROR):
            result = await run_flow(service, conn, lookup=[stored_lookup(B_KEY)])
        assert result["error_code"] == "invoice_issuer_resolution_failed"
        assert "incomplete_stored_issuer" in result["error"]
        assert_no_writes(service, conn, invalidate)

    @pytest.mark.asyncio
    async def test_invalid_default_entity_fails_legacy_adoption(self, env, monkeypatch, caplog):
        registry = synthetic_entities()
        registry[A_KEY] = InvoiceEntity(**{**registry[A_KEY].__dict__, "tax_id": ""})
        monkeypatch.setattr(entities, "ENTITIES", registry)
        monkeypatch.setattr("backend.services.invoicing.invoice_service.ENTITIES", registry)
        service, invalidate, _ = env
        conn = service.db_pool._conn
        with caplog.at_level(logging.ERROR):
            result = await run_flow(service, conn, lookup=[lookup_row(invoice_row_exists=True)])
        assert result["error_code"] == "invoice_issuer_resolution_failed"
        assert "default_entity_invalid" in result["error"]
        assert_no_writes(service, conn, invalidate)
