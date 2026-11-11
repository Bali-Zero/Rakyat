"""Invoice text: issuer legal names in capitals, service line shows the display name.

Real ReportLab + pypdf for the PDF half; the service half mocks the database,
Drive and email, so nothing is delivered or written.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pypdf import PdfReader

from backend.services.invoicing import entities
from backend.services.invoicing.entities import ENTITIES
from backend.services.invoicing.invoice_generator import _REPORTLAB_AVAILABLE, InvoiceGenerator
from backend.services.invoicing.invoice_service import InvoiceAutomationService

EXPECTED_NAMES = {
    "cimb_bayu_bali_nol": "PT BAYU BALI NOL",
    "permata_bali_nol_konsultan": "PT BALI NOL KONSULTAN",
    "bni_bayu_santero": "CV BAYU SANTERO",
    "mandiri_bali_nol_impresariat": "PT BALI NOL IMPRESARIAT",
}

DISPLAY_NAME = "Working KITAS (Altus/Onshore)"
CODE = "kitas_working_onshore"
POST_CUTOVER = datetime(2027, 1, 2, tzinfo=timezone.utc)


def render(entity, *, practice_type: str, description: str | None = None) -> str:
    if not _REPORTLAB_AVAILABLE:
        pytest.fail("reportlab is required for the invoice text test")
    pdf = InvoiceGenerator().generate(
        practice_id=42,
        client_name="Synthetic Client",
        client_email="client@example.test",
        client_phone=None,
        client_address=None,
        practice_type=practice_type,
        practice_description=description,
        quoted_price=1_000_000,
        notes="Synthetic payment note.",
        entity=entity,
    )
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_registry_has_exactly_the_four_expected_issuers():
    assert {k: e.company_name for k, e in ENTITIES.items()} == EXPECTED_NAMES


def test_bank_account_holders_are_the_four_capital_legal_names():
    assert {k: e.bank_account_name for k, e in ENTITIES.items()} == EXPECTED_NAMES


@pytest.mark.parametrize("key", sorted(EXPECTED_NAMES))
def test_pdf_prints_uppercase_issuer_name_on_letterhead_and_footer(key):
    text = render(ENTITIES[key], practice_type=DISPLAY_NAME)
    name = EXPECTED_NAMES[key]
    lines = [line.strip() for line in text.splitlines()]
    assert name in lines, "letterhead line"
    assert f"{name} | Bali, Indonesia" in text, "footer line"
    holder = f"Account Name:\n{EXPECTED_NAMES[key]}"
    assert holder in text, "the bank holder prints in capitals, same as the legal name"
    assert not re.search(r"\b(PT|CV)\.", text), "no dot after PT/CV anywhere on the page"


@pytest.mark.parametrize("key", sorted(EXPECTED_NAMES))
def test_pdf_description_shows_display_name_not_the_code(key):
    text = render(ENTITIES[key], practice_type=DISPLAY_NAME)
    assert DISPLAY_NAME in text
    assert not re.search(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text), "no snake_case code on the page"


def test_pdf_falls_back_to_the_code_only_when_no_name_is_passed():
    text = render(ENTITIES["cimb_bayu_bali_nol"], practice_type=CODE)
    assert CODE in text


def _service(monkeypatch):
    pool = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
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
    base = "backend.services.invoicing.invoice_service."
    monkeypatch.setattr(base + "get_email_client", AsyncMock(return_value=email_client))
    monkeypatch.setattr(base + "log_email_attempt", AsyncMock(return_value=1))
    monkeypatch.setattr(base + "record_email_result", AsyncMock())
    monkeypatch.setattr(base + "invalidate_crm_stats", AsyncMock())
    with (
        patch(base + "ServiceAccountDriveService", return_value=drive),
        patch(base + "InvoiceGenerator", return_value=gen),
    ):
        service = InvoiceAutomationService(pool)
    return service, conn


def _practice(**overrides):
    data = {
        "id": 1,
        "client_id": 100,
        "practice_type_code": CODE,
        "practice_type_name": DISPLAY_NAME,
        "notes": None,
        "quoted_price": 5_000_000,
        "client_drive_folder_id": "folder-1",
    }
    return {**data, **overrides}


async def _run(service, conn, practice):
    lookup = {
        "db_now": POST_CUTOVER,
        "practice_exists": True,
        "invoice_row_exists": False,
        "issuer_entity_key": None,
        "issuer_routed_at": None,
        "practice_documents": None,
        "invoice_documents": None,
    }
    client = {"id": 100, "full_name": "Synthetic Client", "email": "client@example.test"}
    persisted = {"issuer_entity_key": None, "issuer_routed_at": None}
    conn.fetchrow = AsyncMock(
        side_effect=[
            practice,
            client,
            lookup,
            {"client_id": 100, "quoted_price": 5_000_000},
            persisted,
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return await service.trigger_on_sending_invoice(1, "agent@example.test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, DISPLAY_NAME),
        ({"practice_type_name": None}, CODE),
        ({"practice_type_name": ""}, CODE),
    ],
    ids=["name-present", "name-missing", "name-empty"],
)
async def test_service_hands_the_display_name_to_the_generator(monkeypatch, overrides, expected):
    service, conn = _service(monkeypatch)
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", None)
    await _run(service, conn, _practice(**overrides))
    kwargs = service.invoice_generator.generate.call_args.kwargs
    assert kwargs["practice_type"] == expected
