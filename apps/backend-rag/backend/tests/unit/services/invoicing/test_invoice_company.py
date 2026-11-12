"""Pure rules of invoice-to-company: normalisation, autofill, BILL TO block. Synthetic data."""

from __future__ import annotations

import io

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from backend.app.routers.crm_practices import PracticeCreate
from backend.services.invoicing.invoice_company import (
    autofill_applies,
    normalize_company_name,
    normalize_npwp,
    suggest,
)
from backend.services.invoicing.invoice_generator import InvoiceGenerator


@pytest.mark.parametrize(
    "typed",
    ["PT Synthetic Baru", "pt. synthetic baru", "  PT  SYNTHETIC-BARU ", "Synthetic Baru"],
)
def test_name_normalisation_ignores_prefix_case_and_punctuation(typed: str) -> None:
    assert normalize_company_name(typed) == "syntheticbaru"


def test_normalisation_keeps_distinct_names_distinct_and_empty_is_empty() -> None:
    assert normalize_company_name("PT Synthetic Baru") != normalize_company_name(
        "PT Synthetic Lama"
    )
    assert normalize_company_name("PT.") == ""
    assert normalize_company_name(None) == ""


def test_npwp_digits_only_and_short_values_never_match() -> None:
    assert normalize_npwp("01.234.567.8-901.000") == "012345678901000"
    assert normalize_npwp("0123 4567") is None
    assert normalize_npwp(None) is None


@pytest.mark.parametrize(
    "category,expected",
    [
        ("tax", True),
        ("tax_annual", True),
        ("tax_monthly_bundled", True),
        ("company", True),
        ("corporate", True),
        ("kitas", False),
        ("single_entry_visa", False),
        ("other", False),
        (None, False),
    ],
)
def test_autofill_categories(category: str | None, expected: bool) -> None:
    assert autofill_applies(category) is expected


def link(company_id: int, role: str = "shareholder", **extra: object) -> dict:
    return {
        "company_id": company_id,
        "company_name": f"PT Synthetic {company_id}",
        "npwp_company": None,
        "role": role,
        "status": "active",
        "end_date": None,
        **extra,
    }


def test_no_link_is_free_text() -> None:
    result = suggest([], "tax")
    assert (result.autofill, result.suggested_company_id, result.companies) == (True, None, [])


def test_one_active_link_prefills_whatever_the_role() -> None:
    assert suggest([link(7, "komisaris")], "tax").suggested_company_id == 7


def test_ended_and_inactive_links_do_not_count() -> None:
    links = [link(1), link(2, status="inactive"), link(3, end_date="2020-01-01")]
    assert [c["company_id"] for c in suggest(links, "tax").companies] == [1]
    assert suggest(links, "tax").suggested_company_id == 1


def test_several_companies_suggest_only_a_single_director_company() -> None:
    one_director = suggest([link(1, "komisaris"), link(2, "Direktur Utama")], "company")
    assert one_director.suggested_company_id == 2
    assert len(one_director.companies) == 2
    assert suggest([link(1, "director"), link(2, "DIREKTUR")], "tax").suggested_company_id is None
    assert (
        suggest([link(1, "shareholder"), link(2, "komisaris")], "tax").suggested_company_id is None
    )


def test_duplicate_links_to_one_company_are_one_company_and_a_director_role_wins() -> None:
    result = suggest([link(1, "shareholder"), link(1, "Director")], "tax")
    assert result.suggested_company_id == 1
    assert result.companies[0]["is_director"] is True


def test_non_autofill_category_never_suggests_but_still_lists() -> None:
    result = suggest([link(1)], "kitas")
    assert (result.autofill, result.suggested_company_id, len(result.companies)) == (False, None, 1)


def test_create_rejects_id_and_name_together() -> None:
    with pytest.raises(ValidationError):
        PracticeCreate(
            client_id=1, practice_type_code="tax", invoice_company_id=3, invoice_company_name="PT X"
        )


def render(**kwargs: object) -> str:
    pdf = InvoiceGenerator().generate(
        practice_id=1,
        client_name="Synthetic Person",
        client_email="synthetic.person@example.invalid",
        client_phone="+620000",
        client_address="Synthetic Home Street 1",
        practice_type="Synthetic",
        practice_description="Synthetic",
        quoted_price=1000000.0,
        **kwargs,
    )
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def shows(text: str, needle: str) -> bool:
    """The v4 design prints names in capitals: compare case-insensitively."""
    return needle.upper() in text.upper()


def test_bill_to_block_with_company_has_name_npwp_address_attn_and_no_personal_address() -> None:
    text = render(
        bill_to_company="PT Synthetic <A&B>",
        bill_to_npwp="01.234.567.8-901.000",
        bill_to_address="Synthetic Office Road 9",
    )
    assert shows(text, "PT Synthetic <A&B>")
    assert shows(text, "NPWP: 01.234.567.8-901.000")
    assert shows(text, "Synthetic Office Road 9")
    assert shows(text, "Attn: Synthetic Person")
    assert shows(text, "synthetic.person@example.invalid")
    assert not shows(text, "Synthetic Home Street 1")
    assert "+620000" not in text


def test_bill_to_block_without_company_is_the_individual_block() -> None:
    text = render()
    assert shows(text, "Synthetic Home Street 1")
    assert "+620000" in text
    assert not shows(text, "Attn:")
    assert not shows(text, "NPWP: 01")


def test_company_without_npwp_or_address_prints_neither() -> None:
    text = render(bill_to_company="PT Synthetic Polos")
    assert shows(text, "PT Synthetic Polos")
    assert shows(text, "Attn: Synthetic Person")
    assert not shows(text, "NPWP: None")
    assert "None" not in text


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Bayu Bali", (["bayu", "bali"], None)),
        ("PT.BAYU", (["bayu"], None)),
        ("pt bayu cv", (["bayu"], None)),
        ("bayu, Tbk.", (["bayu"], None)),
        ("pt", ([], None)),
        ("b", ([], None)),
        ("", ([], None)),
        ("01.234.567.8-901", ([], "012345678901")),
        ("12", ([], None)),
    ],
)
def test_parse_search_query(typed: str, expected: tuple[list[str], str | None]) -> None:
    from backend.services.invoicing.invoice_company import parse_search_query

    assert parse_search_query(typed) == expected


def test_match_tier_orders_exact_prefix_contains_and_needs_every_word() -> None:
    from backend.services.invoicing.invoice_company import match_tier

    assert match_tier("bayu", ["bayu"]) == 0
    assert match_tier("bayubalinol", ["bayu"]) == 1
    assert match_tier("bayubalinol", ["bayu", "bali"]) == 1
    assert match_tier("balibayunol", ["bayu", "bali"]) == 2
    assert match_tier("zetabayu", ["bayu"]) == 2
    assert match_tier("bayubalinol", ["bayu", "jakarta"]) is None
    assert match_tier("bayu", []) is None


def test_normalizer_ignores_a_trailing_tbk() -> None:
    from backend.services.invoicing.invoice_company import normalize_company_name

    assert normalize_company_name("PT Maju Jaya, Tbk.") == normalize_company_name("maju jaya")
    assert normalize_company_name("PT Maju (Tbk)") == "maju"
    assert normalize_company_name("PT Tbkmaju") == "tbkmaju"
