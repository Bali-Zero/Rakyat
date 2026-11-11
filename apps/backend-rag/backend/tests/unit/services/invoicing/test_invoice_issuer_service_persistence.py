"""Section-6 service-level tests: midnight boundary and persistence failures.

Companion to test_invoice_issuer_service.py / _failures.py. Email, Drive and
the database are fully mocked: nothing is ever delivered or written.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import InvoiceEntity
from backend.services.invoicing.invoice_service import (
    _INVOICE_UPSERT_PRE158_SQL,
    _INVOICE_UPSERT_SQL,
    InvoiceAutomationService,
)

A_KEY = "cimb_bayu_bali_nol"

POST_CUTOVER = datetime(2027, 1, 2, tzinfo=timezone.utc)


def synthetic_entities() -> dict[str, InvoiceEntity]:
    return {
        A_KEY: InvoiceEntity(
            key=A_KEY,
            company_name="Synthetic Issuer One",
            tax_id="00.000.000.0-000.000",
            address=None,
            bank_account_name="Synthetic Holder One",
            bank_account_number="0000000001",
            bank_name="Synthetic Bank One",
            bank_branch="Synthetic Branch One",
            bank_swift="SYNTHIDJA",
        )
    }


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
        "db_now": POST_CUTOVER,
        "practice_exists": True,
        "invoice_row_exists": False,
        "issuer_entity_key": None,
        "issuer_routed_at": None,
        "practice_documents": None,
    }
    return {**row, **overrides}


def stored_lookup(key, routed_at, db_now=POST_CUTOVER):
    return lookup_row(
        db_now=db_now,
        invoice_row_exists=True,
        issuer_entity_key=key,
        issuer_routed_at=routed_at,
    )


def binding_row(key, routed_at):
    return {"issuer_entity_key": key, "issuer_routed_at": routed_at}


def practice_row():
    return {"client_id": 100, "quoted_price": 5_000_000}


def run_flow(service, conn, *, practice=None, lookup=(), persisted=None):
    side = [practice or practice_data(), client_data(), *lookup, practice_row(), persisted]
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


class _SqlStateError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(f"synthetic pg error {sqlstate}")
        self.sqlstate = sqlstate


# ============================================================
# Midnight boundary: pre-cutover lookup instant is what persists
# ============================================================


class TestMidnightBinding:
    @pytest.mark.asyncio
    async def test_midnight_persists_original_db_binding(self, env, synth, monkeypatch):
        monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
        service, _, _ = env
        conn = service.db_pool._conn
        before = datetime(2026, 11, 11, 15, 59, 59, tzinfo=timezone.utc)
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code="visa_c1_tourism"),
            lookup=[lookup_row(db_now=before)],
            persisted=binding_row(A_KEY, before),
        )
        assert result["success"] is True
        assert generator_entity(service) is synth[A_KEY]
        assert upsert_call(conn)[13] == before, "the pre-cutover lookup instant must persist"
        result2 = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code="visa_c1_tourism"),
            lookup=[stored_lookup(A_KEY, before)],
            persisted=binding_row(A_KEY, before),
        )
        assert result2["success"] is True
        assert generator_entity(service) is synth[A_KEY]
        assert upsert_call(conn)[13] == before


# ============================================================
# Guarded upsert rejection and persistence failures
# ============================================================


class TestPersistenceFailures:
    @pytest.mark.asyncio
    async def test_guard_rejection_blocks_all_writes(self, env):
        service, invalidate, _ = env
        conn = service.db_pool._conn
        result = await run_flow(service, conn, lookup=[lookup_row()], persisted=None)
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_persistence_failed"
        assert "issuer_conflict" in result["error"]
        assert result["delivery_may_have_occurred"] is True
        assert conn.execute.call_count == 0, "no documents write, no activity log"
        invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_different_returned_key_is_conflict(self, env):
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            lookup=[lookup_row()],
            persisted=binding_row("bni_bayu_santero", POST_CUTOVER),
        )
        assert result["success"] is False
        assert "issuer_conflict" in result["error"]
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_naive_returned_routed_at_is_invalid_binding(self, env):
        service, _, _ = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            lookup=[lookup_row()],
            persisted={"issuer_entity_key": A_KEY, "issuer_routed_at": datetime(2027, 1, 2)},
        )
        assert result["success"] is False
        assert "invalid_persisted_binding" in result["error"]
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_write_failed_without_schema_fallback(self, env):
        service, _, _ = env
        conn = service.db_pool._conn
        conn.fetchrow = AsyncMock(
            side_effect=[
                practice_data(),
                client_data(),
                lookup_row(),
                practice_row(),
                RuntimeError("gone"),
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert "write_failed" in result["error"]
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_42703_in_both_variants_is_write_failed(self, env):
        service, _, _ = env
        conn = service.db_pool._conn
        err = _SqlStateError("42703")
        conn.fetchrow = AsyncMock(
            side_effect=[practice_data(), client_data(), lookup_row(), practice_row(), err, err]
        )
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert "write_failed" in result["error"]
        assert conn.execute.call_count == 0
        calls = [
            c
            for c in conn.fetchrow.call_args_list
            if c.args and c.args[0] in (_INVOICE_UPSERT_SQL, _INVOICE_UPSERT_PRE158_SQL)
        ]
        assert len(calls) == 2, "normal and pre-158 variants must both have been attempted"

    @pytest.mark.asyncio
    async def test_persistence_failure_after_send_is_critical_and_not_retried(self, env, caplog):
        service, _, email_client = env
        conn = service.db_pool._conn
        conn.fetchrow = AsyncMock(
            side_effect=[
                practice_data(),
                client_data(),
                lookup_row(),
                practice_row(),
                RuntimeError("gone"),
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        with caplog.at_level(logging.CRITICAL):
            result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_persistence_failed"
        assert result["delivery_may_have_occurred"] is True
        criticals = [
            r
            for r in caplog.records
            if r.name == "backend.services.invoicing.invoice_service"
            and r.levelno == logging.CRITICAL
        ]
        assert len(criticals) == 1
        context = criticals[0].context  # type: ignore[attr-defined]
        assert context["event"] == "invoice_issuer_persistence_failed"
        assert context["reason"] == "write_failed"
        assert context["email_attempted"] is True
        assert context["email_sent"] is True
        assert context["accounting_notified"] is True
        assert context["drive_uploaded"] is True
        assert context["delivery_may_have_occurred"] is True
        assert context["issuer_entity_key"] == A_KEY
        assert context["source"] == "new"
        assert service.invoice_generator.generate.call_count == 1, "no retry of the delivery"
        assert email_client.post.await_count == 2, "client + accounting email only"


class TestPersistenceFailuresBeforeAndAfterTheUpsert:
    """Any database failure while persisting after delivery is loud and critical."""

    @staticmethod
    def _criticals(caplog):
        return [
            r
            for r in caplog.records
            if r.name == "backend.services.invoicing.invoice_service"
            and r.levelno == logging.CRITICAL
        ]

    @pytest.mark.asyncio
    async def test_failure_of_the_pre_upsert_practice_select_is_critical(self, env, caplog):
        service, _, _ = env
        conn = service.db_pool._conn
        conn.fetchrow = AsyncMock(
            side_effect=[
                practice_data(),
                client_data(),
                lookup_row(),
                _SqlStateError("57P01"),
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        with caplog.at_level(logging.CRITICAL):
            result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_persistence_failed"
        assert result["delivery_may_have_occurred"] is True
        criticals = self._criticals(caplog)
        assert len(criticals) == 1
        assert criticals[0].context["reason"] == "write_failed"  # type: ignore[attr-defined]
        assert criticals[0].context["sqlstate"] == "57P01"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_failure_to_acquire_a_connection_for_persistence_is_critical(self, env, caplog):
        service, _, _ = env
        conn = service.db_pool._conn
        conn.fetchrow = AsyncMock(side_effect=[practice_data(), client_data(), lookup_row()])
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        real_acquire = service.db_pool.acquire
        calls = {"n": 0}

        def acquire():
            calls["n"] += 1
            if calls["n"] >= 4:
                raise OSError("pool closed")
            return real_acquire()

        service.db_pool.acquire = acquire
        with caplog.at_level(logging.CRITICAL):
            result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_persistence_failed"
        assert len(self._criticals(caplog)) == 1

    @pytest.mark.asyncio
    async def test_failure_of_the_documents_update_after_the_upsert_is_critical(self, env, caplog):
        service, _, _ = env
        conn = service.db_pool._conn
        conn.fetchrow = AsyncMock(
            side_effect=[
                practice_data(),
                client_data(),
                lookup_row(),
                practice_row(),
                binding_row(A_KEY, POST_CUTOVER),
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(side_effect=_SqlStateError("40001"))
        with caplog.at_level(logging.CRITICAL):
            result = await service.trigger_on_sending_invoice(1, "agent@example.test")
        assert result["success"] is False
        assert result["error_code"] == "invoice_issuer_persistence_failed"
        assert len(self._criticals(caplog)) == 1
