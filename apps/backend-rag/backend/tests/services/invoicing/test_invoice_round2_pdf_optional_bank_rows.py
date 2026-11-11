"""Round-2 R3: bank_branch and bank_swift are optional rows on the invoice PDF.

The other three bank rows (Account Name / Account Number / Bank Name) always
print; the address line prints when set.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from backend.services.invoicing.entities import InvoiceEntity
from backend.services.invoicing.invoice_generator import _REPORTLAB_AVAILABLE, InvoiceGenerator


def _entity(*, bank_branch: str | None, bank_swift: str | None) -> InvoiceEntity:
    return InvoiceEntity(
        key="permata_bali_nol_konsultan",
        company_name="Synthetic Issuer Four",
        tax_id="33.333.333.3-333.333",
        address="Synthetic Street Four 4, Synthetic City",
        bank_account_name="Synthetic Holder Four",
        bank_account_number="0000000004",
        bank_name="Synthetic Bank Four",
        bank_branch=bank_branch,
        bank_swift=bank_swift,
    )


@pytest.fixture
def generator() -> InvoiceGenerator:
    if not _REPORTLAB_AVAILABLE:
        pytest.fail("reportlab is required for the optional-bank-rows PDF test")
    return InvoiceGenerator()


def _render(generator: InvoiceGenerator, entity: InvoiceEntity) -> str:
    pdf_bytes = generator.generate(
        practice_id=43,
        client_name="Synthetic Client",
        client_email="client@example.test",
        client_phone=None,
        client_address=None,
        practice_type="TAX",
        practice_description="Synthetic tax service",
        quoted_price=2_000_000,
        notes=None,
        entity=entity,
    )
    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def _assert_core_bank_rows(text: str, entity: InvoiceEntity) -> None:
    assert f"Account Name:\n{entity.bank_account_name}" in text
    assert f"Account Number:\n{entity.bank_account_number}" in text
    assert f"Bank Name:\n{entity.bank_name}" in text


def test_pdf_omits_branch_and_swift_rows_when_unset(generator: InvoiceGenerator) -> None:
    entity = _entity(bank_branch=None, bank_swift=None)
    text = _render(generator, entity)
    assert "Bank Branch:" not in text
    assert "Swift Code:" not in text
    _assert_core_bank_rows(text, entity)


def test_pdf_treats_blank_branch_and_swift_as_unset(generator: InvoiceGenerator) -> None:
    entity = _entity(bank_branch="   ", bank_swift=" ")
    text = _render(generator, entity)
    assert "Bank Branch:" not in text
    assert "Swift Code:" not in text
    _assert_core_bank_rows(text, entity)


def test_pdf_renders_branch_and_swift_rows_when_set(generator: InvoiceGenerator) -> None:
    entity = _entity(bank_branch="Synthetic Branch Four", bank_swift="SYNTHIDJD")
    text = _render(generator, entity)
    assert f"Bank Branch:\n{entity.bank_branch}" in text
    assert f"Swift Code:\n{entity.bank_swift}" in text
    _assert_core_bank_rows(text, entity)


def test_address_line_prints_when_set(generator: InvoiceGenerator) -> None:
    entity = _entity(bank_branch=None, bank_swift=None)
    text = _render(generator, entity)
    assert entity.address in text
