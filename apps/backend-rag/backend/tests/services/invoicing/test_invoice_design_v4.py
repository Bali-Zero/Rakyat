"""Real-ReportLab + pypdf proof of the v4 invoice design for every production issuer.

The registry is only read at runtime: no real company value is written in this file.
"""

from __future__ import annotations

import dataclasses
import io
import re

import pytest
from pypdf import PdfReader

from backend.services.invoicing.entities import ENTITIES, InvoiceEntity
from backend.services.invoicing.invoice_generator import (
    _REPORTLAB_AVAILABLE,
    InvoiceGenerator,
    amount_in_words,
    group_npwp,
)

ADDRESS = "Jalan Raya Anyar No. 2, Kerobokan, Kuta Utara, Kab. Badung, Bali"
LONG_NAME = "PT SYNTHETIC LONG CLIENT NAME FOR WRAPPING TESTS INTERNATIONAL TRADING AND SERVICES"


@pytest.fixture(scope="module")
def generator() -> InvoiceGenerator:
    if not _REPORTLAB_AVAILABLE:
        pytest.fail("reportlab is required for the invoice design test")
    return InvoiceGenerator()


def make_pdf(generator: InvoiceGenerator, entity: InvoiceEntity, **overrides) -> bytes:
    args = {
        "practice_id": 4242,
        "client_name": "Synthetic Client",
        "client_email": "client@example.test",
        "client_phone": None,
        "client_address": None,
        "practice_type": "synthetic_code",
        "practice_description": "Synthetic Service Name",
        "quoted_price": 5_000_000,
        "notes": "Synthetic payment note.",
        "entity": entity,
    }
    args.update(overrides)
    return generator.generate(**args)


def read(pdf: bytes) -> tuple[PdfReader, str]:
    reader = PdfReader(io.BytesIO(pdf))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


@pytest.mark.parametrize("key", sorted(ENTITIES))
def test_each_issuer_renders_the_v4_content_on_one_page(generator, key):
    entity = ENTITIES[key]
    reader, text = read(make_pdf(generator, entity))
    assert len(reader.pages) == 1
    assert f"ISSUED BY\n{entity.company_name}\n{ADDRESS}\nNPWP: " in text
    assert re.search(r"NPWP: \d{4} \d{4} \d{4} \d{4}\n", text), "NPWP in groups of four"
    assert f"NPWP: {group_npwp(entity.tax_id)}" in text
    assert f"PAY TO\n{entity.bank_name}\n{entity.bank_account_name}" in text
    assert f"ACCOUNT NUMBER\n{entity.bank_account_number}" in text
    number = re.search(r"INV-\d{6}-04242", text)
    assert number, "today's invoice numbering"
    assert f"Reference: {number.group(0)}" in text, "bank reference carries the invoice number"
    assert "TOTAL DUE\n IDR 5,000,000" in text
    assert "Amount in words: Five million Rupiah" in text
    assert "Synthetic Service Name" in text
    assert "BILL TO\nSYNTHETIC CLIENT\nclient@example.test" in text
    assert f"{entity.company_name}  ·  " in text, "footer line"


def test_no_draft_mark_no_tax_row_no_pph_no_zero_rows(generator):
    _, text = read(make_pdf(generator, ENTITIES["permata_bali_nol_konsultan"]))
    for forbidden in ("DRAFT", "NOT VALID", "Tax (0%)", "PPh", "Subtotal", "Discount"):
        assert forbidden not in text, forbidden


def test_empty_bank_fields_are_not_rendered_and_filled_ones_are(generator):
    base = ENTITIES["cimb_bayu_bali_nol"]
    empty = dataclasses.replace(base, bank_branch="", bank_swift=None)
    _, text = read(make_pdf(generator, empty))
    assert "BANK BRANCH" not in text and "SWIFT CODE" not in text
    only_swift = dataclasses.replace(base, bank_branch="  ", bank_swift="SYNTHSWIFT")
    _, text = read(make_pdf(generator, only_swift))
    assert "BANK BRANCH" not in text
    assert "SWIFT CODE\nSYNTHSWIFT" in text
    both = dataclasses.replace(base, bank_branch="Synthetic Branch", bank_swift="SYNTHSWIFT")
    _, text = read(make_pdf(generator, both))
    assert "BANK BRANCH\nSynthetic Branch" in text and "SWIFT CODE\nSYNTHSWIFT" in text


def test_discount_rows_print_only_when_there_is_a_discount(generator):
    entity = ENTITIES["cimb_bayu_bali_nol"]
    _, text = read(
        make_pdf(generator, entity, discount_amount=500_000, discount_reason="Synthetic promo")
    )
    assert "Subtotal" in text and "Discount (Synthetic promo)" in text
    assert "TOTAL DUE\n IDR 4,500,000" in text
    assert "Four million five hundred thousand Rupiah" in text
    _, none = read(make_pdf(generator, entity, discount_amount=0))
    assert "Discount" not in none


def _heading_y(pdf: bytes) -> float:
    found: list[float] = []

    def visit(text, cm, _tm, _font_dict, _font_size):
        if text.strip() == "PAYMENT INFORMATION":
            found.append(cm[5])

    PdfReader(io.BytesIO(pdf)).pages[0].extract_text(visitor_text=visit)
    assert len(found) == 1
    return found[0]


def test_unset_bank_fields_leave_no_empty_row_in_the_pay_to_grid(generator):
    base = ENTITIES["cimb_bayu_bali_nol"]
    empty = dataclasses.replace(base, bank_branch="", bank_swift=None)
    filled = dataclasses.replace(base, bank_branch="Synthetic Branch", bank_swift="SYNTHSWIFT")
    flat = _heading_y(make_pdf(generator, empty))
    tall = _heading_y(make_pdf(generator, filled))
    assert tall - flat > 30, "the second grid row exists only when branch or SWIFT is set"


def test_description_falls_back_to_practice_type_when_blank(generator):
    _, text = read(make_pdf(generator, ENTITIES["cimb_bayu_bali_nol"], practice_description=None))
    assert "synthetic_code" in text


def test_long_client_name_wraps_and_stays_complete_on_one_page(generator):
    reader, text = read(make_pdf(generator, ENTITIES["bni_bayu_santero"], client_name=LONG_NAME))
    assert len(reader.pages) == 1
    assert all(word in text for word in LONG_NAME.split())


def test_stress_flows_to_two_pages_with_the_masthead_and_payment_block_intact(generator):
    entity = ENTITIES["mandiri_bali_nol_impresariat"]
    long_description = " ".join(
        f"Synthetic line {i} of a very long service description." for i in range(1, 30)
    )
    reader, text = read(
        make_pdf(
            generator,
            entity,
            client_name=LONG_NAME,
            client_address=" ".join(
                ["Synthetic Street 1, Synthetic City, Synthetic Province."] * 6
            ),
            practice_description=long_description,
        )
    )
    assert len(reader.pages) == 2
    first, second = (page.extract_text() for page in reader.pages)
    assert "INVOICE" in first and "INVOICE" in second, "masthead on each page"
    assert f"{entity.company_name}  ·  " in first and f"{entity.company_name}  ·  " in second
    assert "TOTAL DUE" in text and f"ACCOUNT NUMBER\n{entity.bank_account_number}" in text
    assert text.count("PAYMENT INFORMATION") == 1


def _fonts(page):
    return [f.get_object() for f in page["/Resources"]["/Font"].values()]


def test_montserrat_is_embedded_and_red_is_not_used(generator):
    reader, _ = read(make_pdf(generator, ENTITIES["cimb_bayu_bali_nol"]))
    page = reader.pages[0]
    fonts = {str(f.get("/BaseFont", "")) for f in _fonts(page)}
    assert any("Montserrat" in f for f in fonts), fonts
    content = page.get_contents().get_data().decode("latin-1")
    assert ".956863 .768627 .188235 rg" in content, "brand yellow is drawn"
    assert ".784314 .062745 .180392" not in content, "no red outside status"


@pytest.mark.parametrize(
    ("value", "words"),
    [
        (0, "Zero"),
        (7, "Seven"),
        (19, "Nineteen"),
        (20, "Twenty"),
        (105, "One hundred five"),
        (36_000_000, "Thirty-six million"),
        (1_000_001, "One million one"),
        (
            2_345_678_901,
            "Two billion three hundred forty-five million six hundred seventy-eight "
            "thousand nine hundred one",
        ),
    ],
)
def test_amount_in_words(value, words):
    assert amount_in_words(value) == words


@pytest.mark.parametrize(
    ("raw", "grouped"),
    [
        ("0000000000000000", "0000 0000 0000 0000"),
        ("00.000.000.0-000.000", "00.000.000.0-000.000"),
        ("0000 0000 0000 0000", "0000 0000 0000 0000"),
        (None, ""),
    ],
)
def test_group_npwp(raw, grouped):
    assert group_npwp(raw) == grouped
