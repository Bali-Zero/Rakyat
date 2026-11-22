"""An invoice prints a name its fonts lack in base letters, and refuses a buyer it cannot print.

The bundled fonts cover Basic Latin, Latin-1 and some punctuation. Before, a letter outside
them was dropped without a word: a Polish name lost half its letters and a Cyrillic one left
the BILL TO block empty, on an invoice that was sent and reported as a success.
"""

from __future__ import annotations

import io
import logging

import pytest
from pypdf import PdfReader

from backend.services.invoicing.entities import CIMB_BAYU_BALI_NOL
from backend.services.invoicing.invoice_generator import (
    _REPORTLAB_AVAILABLE,
    InvoiceGenerator,
    InvoiceTextNotPrintable,
    _fold,
    _why_not_printable,
)

LOGGER = "backend.services.invoicing.invoice_generator"
# Pangram words, not people: every letter the fonts lack in five alphabets.
POLISH = "Za\u017c\u00f3\u0142\u0107 G\u0119\u015bl\u0105 Ja\u017a\u0144"
CZECH = "P\u0159\u00edli\u0161 \u017dlu\u0165ou\u010dk\u00fd K\u016f\u0148"
TURKISH = "\u015ei\u015fli I\u011fd\u0131r \u0130\u00e7\u00f6z"
LATVIAN = "Gl\u0101\u017e\u0161\u0137\u016b\u0146a R\u016b\u0137\u012b\u0161i"
CROATIAN = "\u0110akovo \u0160\u0107u\u0107urio"
CYRILLIC = "\u0421\u044a\u0435\u0448\u044c \u0415\u0449\u0451"
FLOWER = "\U0001f338"


@pytest.fixture(scope="module")
def generator() -> InvoiceGenerator:
    if not _REPORTLAB_AVAILABLE:
        pytest.fail("reportlab is required for the invoice tests")
    return InvoiceGenerator()


def pdf_text(generator: InvoiceGenerator, **overrides) -> str:
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
        "entity": CIMB_BAYU_BALI_NOL,
    }
    args.update(overrides)
    reader = PdfReader(io.BytesIO(generator.generate(**args)))
    return "\n".join(page.extract_text() for page in reader.pages)


# ── a letter the fonts lack prints as its base letter ────────────────────────


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        (POLISH, "ZAZ\u00d3LC GESLA JAZN"),
        (CZECH, "PR\u00cdLIS ZLUTOUCK\u00dd KUN"),
        (TURKISH, "SISLI IGDIR I\u00c7\u00d6Z"),
        (LATVIAN, "GLAZSKUNA RUKISI"),
        (CROATIAN, "DAKOVO SCUCURIO"),
        ("\u00ffvonne \u0153uvre", "YVONNE \u0152UVRE"),
        ("Synthetic \u0192oo", "SYNTHETIC FOO"),
        ("\u021aar\u0103 Synthetic", "TAR\u0102 SYNTHETIC"),
    ],
    ids=[
        "polish",
        "czech",
        "turkish",
        "latvian",
        "croatian",
        "a capital the fonts lack",
        "a capital the fold does not know",
        "a capital the fonts have and a small letter they lack",
    ],
)
def test_a_buyer_name_prints_in_base_letters(
    generator: InvoiceGenerator, name: str, printed: str
) -> None:
    assert printed in pdf_text(generator, client_name=name)


def test_a_name_the_fonts_cover_prints_as_it_is(generator: InvoiceGenerator) -> None:
    assert "JOS\u00c9 M\u00dcLLER" in pdf_text(generator, client_name="Jos\u00e9 M\u00fcller")
    assert _fold("Synthetic Client & <Co> \u2014 \u201cquoted\u201d") == (
        "Synthetic Client & <Co> \u2014 \u201cquoted\u201d",
        [],
    )


def test_punctuation_the_fonts_lack_prints_as_its_plain_form(generator: InvoiceGenerator) -> None:
    company = "PT \u201fKopi\u2011Kopi\u201f \u2015 O\u201bNeil \u2116 5"

    text = pdf_text(generator, bill_to_company=company)

    assert 'PT "KOPI-KOPI" - O\'NEIL NO 5' in text


def test_a_symbol_is_left_out_and_the_name_closes_up(generator: InvoiceGenerator) -> None:
    text = pdf_text(generator, client_name=f"Anna {FLOWER} Maria \u2728")

    assert "ANNA MARIA" in text
    assert _fold(f"Anna {FLOWER} Maria \u2728") == ("Anna Maria", [])
    assert _fold("\U0001f468\u200d\U0001f469\u200d\U0001f467 Tiga \U0001f1ee\U0001f1e9") == (
        "Tiga",
        [],
    )


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("e\u0301", "\u00e9"),
        ("s\u030c", "s"),
        ("\uff11\uff12 \u0663", "12 3"),
        ("a\u202fb\u2028c", "a b c"),
        ("\ufb01n", "fin"),
        ("co\u00adop", "coop"),
        ("line one\nline two", "line one\nline two"),
    ],
    ids=[
        "a split accent is joined",
        "a split caron is dropped",
        "digits of another script",
        "odd spaces",
        "a ligature",
        "a soft hyphen",
        "a line break",
    ],
)
def test_what_prints_in_place_of_what(text: str, printed: str) -> None:
    assert _fold(text) == (printed, [])


# ── a buyer the fonts cannot print is refused ────────────────────────────────


@pytest.mark.parametrize(
    ("name", "script", "letters"),
    [
        (CYRILLIC, "CYRILLIC", 8),
        ("\u5c71\u7530", "CJK", 2),
        ("\u0627\u062e\u062a\u0628\u0627\u0631", "ARABIC", 6),
        ("\u0e17\u0e14\u0e2a\u0e2d\u0e1a", "THAI", 5),
        ("\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac", "GREEK", 8),
        (f"Synthetic {CYRILLIC}", "CYRILLIC", 8),
    ],
    ids=["cyrillic", "cjk", "arabic", "thai", "greek", "half latin"],
)
def test_a_buyer_name_in_another_script_is_refused_with_a_reason(
    generator: InvoiceGenerator,
    caplog: pytest.LogCaptureFixture,
    name: str,
    script: str,
    letters: int,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(InvoiceTextNotPrintable) as refusal,
    ):
        pdf_text(generator, client_name=name)

    reason = str(refusal.value)
    assert reason == (
        f"the buyer name has {letters} letter(s) the invoice fonts cannot print ({script}); "
        "write it in basic Latin letters on the record and generate the invoice again"
    )
    # The invoice service catches a ValueError as a failed PDF and reports the reason.
    assert isinstance(refusal.value, ValueError)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert [record.getMessage() for record in errors] == [
        f"Invoice for practice 4242 refused: {reason}"
    ]
    assert all(record.getMessage().isascii() for record in caplog.records)


@pytest.mark.parametrize(
    "name",
    [f"{FLOWER}{FLOWER}", f"{FLOWER} -", "\u00a9", "-", "(&)"],
    ids=["symbols", "a symbol and a dash", "a sign the fonts have", "a dash", "punctuation"],
)
def test_a_buyer_name_with_no_letter_or_digit_is_refused(
    generator: InvoiceGenerator, name: str
) -> None:
    with pytest.raises(InvoiceTextNotPrintable) as refusal:
        pdf_text(generator, client_name=name)

    assert str(refusal.value) == (
        "the buyer name has no letter or digit the invoice fonts can print; "
        "write it in basic Latin letters on the record and generate the invoice again"
    )


# ── capitals lose nothing ────────────────────────────────────────────────────


def test_every_letter_that_prints_as_written_prints_in_capitals() -> None:
    # BILL TO shows the buyer in capitals. Raising a letter must never be what refuses a
    # name: checked for every letter there is, not for a list of known ones.
    refused_only_in_capitals = [
        f"U+{code:04X}"
        for code in range(0x20, 0x30000)
        if chr(code).isalpha()
        and _why_not_printable(chr(code), "buyer name") is None
        and _why_not_printable(chr(code), "buyer name", capitals=True) is not None
    ]

    assert refused_only_in_capitals == []


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("Synthetic Client & <Co>", "SYNTHETIC CLIENT & <CO>"),
        ("\u0192oo", "FOO"),
        ("stra\u00dfe", "STRASSE"),
        ("\u00b5 meter", "\u00b5 METER"),
        ("\u021aar\u0103", "TAR\u0102"),
        ("\u0138ra", "KRA"),
        ("\U0001d4b6\U0001d4c3\U0001d4c3\U0001d4b6", "ANNA"),
        ("e\u0301cole", "\u00c9COLE"),
    ],
    ids=[
        "plain",
        "a capital the fold does not know",
        "a capital of two letters",
        "a capital in another script stays small",
        "a capital the fonts have",
        "a letter with no capital",
        "letters in a script style",
        "a split accent",
    ],
)
def test_the_buyer_line_is_the_name_in_capitals(name: str, line: str) -> None:
    assert _fold(name, capitals=True) == (line, [])


# ── with a billing company, the contact line ─────────────────────────────────


def test_the_contact_of_a_billing_company_prints_as_before(generator: InvoiceGenerator) -> None:
    text = pdf_text(generator, bill_to_company="PT Synthetic Buyer")

    assert "PT SYNTHETIC BUYER" in text
    assert "Attn: Synthetic Client" in text


def test_a_contact_name_prints_in_base_letters(generator: InvoiceGenerator) -> None:
    text = pdf_text(generator, bill_to_company="PT Synthetic Buyer", client_name=POLISH)

    assert "Attn: Zaz\u00f3lc Gesla Jazn" in text


@pytest.mark.parametrize("contact", [CYRILLIC, f"Synthetic {CYRILLIC}", FLOWER, f"{FLOWER} -"])
def test_a_contact_that_cannot_print_whole_is_left_off_and_the_invoice_goes_out(
    generator: InvoiceGenerator, caplog: pytest.LogCaptureFixture, contact: str
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        text = pdf_text(generator, bill_to_company="PT Synthetic Buyer", client_name=contact)

    assert "PT SYNTHETIC BUYER" in text
    assert "Attn" not in text
    assert "client@example.test" in text
    assert [record.getMessage() for record in caplog.records] == [
        "Invoice for practice 4242: the contact name cannot be printed whole; "
        "the Attn line is left out"
    ]


# ── the other texts ──────────────────────────────────────────────────────────


def test_other_texts_print_what_they_can_and_the_log_says_how_much_is_missing(
    generator: InvoiceGenerator, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        text = pdf_text(
            generator,
            client_address=f"{CYRILLIC} 5, Bali",
            practice_description=f"Visa {FLOWER} E33G",
            discount_amount=500_000,
            discount_reason="\u0141oyalty",
        )

    assert "SYNTHETIC CLIENT" in text
    assert "5, Bali" in text
    assert "Visa E33G" in text
    assert "Discount (Loyalty)" in text
    assert [record.getMessage() for record in caplog.records] == [
        "Invoice text has 8 letter(s) the invoice fonts cannot print; left out"
    ]
