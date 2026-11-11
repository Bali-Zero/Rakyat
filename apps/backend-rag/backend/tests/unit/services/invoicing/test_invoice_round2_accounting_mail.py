"""Round-2 R5(a) + flow parity: the accounting mail and the full trigger flow.

The accounting notification shows the issuing company name and the bank name,
never the account number, and its Amount is the final post-discount total.
A TAX practice (``tax_monthly_basic_0_50``) with routing active produces the
same observable steps as a visa practice: client mail, accounting mail, and
persisted issuer binding with the routed synthetic entity.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, InvoiceEntity
from backend.services.invoicing.invoice_service import (
    _INVOICE_UPSERT_SQL,
    InvoiceAutomationService,
)

B_KEY = "mandiri_bali_nol_impresariat"
D_KEY = "permata_bali_nol_konsultan"

POST_CUTOVER = datetime(2027, 1, 2, tzinfo=timezone.utc)
QUOTED = 5_000_000.0
DISCOUNT = 500_000.0
FINAL = QUOTED - DISCOUNT


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
    a = InvoiceEntity(key=DEFAULT_ENTITY_KEY, company_name="Synthetic Issuer One", **base)
    b = InvoiceEntity(
        key=B_KEY,
        company_name="Synthetic Issuer Two",
        **{**base, "tax_id": "11.111.111.1-111.111", "address": "Synthetic Street Two"},
    )
    d = InvoiceEntity(
        key=D_KEY,
        company_name="Synthetic Issuer Four",
        **{
            **base,
            "tax_id": "33.333.333.3-333.333",
            "bank_account_number": "0000000004",
            "bank_branch": None,
            "bank_swift": None,
        },
    )
    return {a.key: a, b.key: b, d.key: d}


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
        "quoted_price": QUOTED,
        "discount_amount": DISCOUNT,
        "discount_reason": "Synthetic repeat-client discount",
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


def binding_row(key, routed_at):
    return {"issuer_entity_key": key, "issuer_routed_at": routed_at}


def practice_row():
    return {"client_id": 100, "quoted_price": QUOTED, "discount_amount": DISCOUNT}


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


def _assert_accounting_body(body: str, entity: InvoiceEntity) -> None:
    assert entity.company_name in body
    assert entity.bank_name in body
    assert entity.bank_account_number not in body
    assert f"IDR {FINAL:,.0f}" in body
    assert f"IDR {QUOTED:,.0f}" not in body


# ============================================================
# Accounting mail content (direct)
# ============================================================


class TestAccountingNotificationContent:
    @pytest.mark.asyncio
    async def test_mail_shows_company_bank_name_final_amount_no_account_number(self, env, synth):
        service, _, email_client = env
        entity = synth[D_KEY]
        result = await service._send_accounting_notification(
            client_data=client_data(),
            practice_data=practice_data(),
            invoice_number="INV-R2-1",
            pdf_bytes=b"%PDF-1.4 synthetic",
            filename="Invoice_INV-R2-1.pdf",
            entity=entity,
            amount=FINAL,
        )
        assert result is True
        payload = email_client.post.call_args.kwargs["json"]
        assert payload["to"] == ACCOUNTING_EMAIL
        _assert_accounting_body(payload["body"], entity)


# ============================================================
# Full-flow parity: visa practice vs TAX practice, routing active
# ============================================================


class TestTriggerFlowParity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code", "key"),
        [("visa_c1_tourism", B_KEY), ("tax_monthly_basic_0_50", D_KEY)],
        ids=["visa_practice", "tax_practice"],
    )
    async def test_flow_sends_client_and_accounting_and_persists_routed_entity(
        self, env, synth, monkeypatch, code, key
    ):
        monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
        service, _, email_client = env
        conn = service.db_pool._conn
        result = await run_flow(
            service,
            conn,
            practice=practice_data(practice_type_code=code),
            lookup=[lookup_row(db_now=POST_CUTOVER)],
            persisted=binding_row(key, POST_CUTOVER),
        )
        assert result["success"] is True
        assert result["email_sent"] is True
        assert result["accounting_notified"] is True
        assert generator_entity(service) is synth[key]
        args = upsert_call(conn)
        assert args[12] == key
        assert args[13] == POST_CUTOVER
        assert args[5] == FINAL, "invoices.amount_idr stores the post-discount total"

        assert email_client.post.await_count == 2
        client_payload = email_client.post.await_args_list[0].kwargs["json"]
        assert client_payload["to"] == "client@example.test"
        assert f"IDR {FINAL:,.0f}" in client_payload["body"]
        accounting_payload = email_client.post.await_args_list[1].kwargs["json"]
        assert accounting_payload["to"] == ACCOUNTING_EMAIL
        _assert_accounting_body(accounting_payload["body"], synth[key])
        assert (
            accounting_payload["attachments"][0]["content"]
            == base64.b64encode(b"%PDF-1.4 synthetic").decode()
        )
