from __future__ import annotations

import pytest

from backend.services.rag.agentic.entity_extractor import EntityExtractionService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_amount"),
    [
        ("my budget is usd 50000", "50000"),
        ("my budget is $ 2,500", "2,500"),
        ("my budget is rp 10.000.000", "10.000.000"),
    ],
)
async def test_extract_entities_reads_budget_currency_markers(
    query: str, expected_amount: str
) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert expected_amount in entities["budget"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "How do I establish a company in Indonesia?",
        "The carport needs 200 tiles before renovation.",
        "An idric 300 sample is unrelated to a budget.",
    ],
)
async def test_extract_entities_ignores_missing_or_fragment_currency_markers(query: str) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert "budget" not in entities


@pytest.mark.asyncio
async def test_extract_entities_does_not_treat_budget_as_kbli_code() -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities("rent a house if my budget is usd 50000")

    assert "kbli_code" not in entities
    assert entities["domain"] != "kbli"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ("what is kbli 56101", "56101"),
        ("KBLI code 62019 requirements", "62019"),
    ],
)
async def test_extract_entities_reads_contextual_kbli_codes(query: str, expected_code: str) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert entities["kbli_code"] == expected_code
    assert entities["domain"] == "kbli"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_domain", "expected_tax_concept", "expected_nationality"),
    [
        ("how do I set up a private limited company", "company", None, None),
        ("is it advisable to buy land via a nominee", "property", None, None),
        ("please explain the usage limit for a PT PMA bank account", "company", None, None),
        ("an island", "general", None, None),
    ],
)
async def test_extract_entities_does_not_read_short_keywords_inside_words(
    query: str,
    expected_domain: str,
    expected_tax_concept: str | None,
    expected_nationality: str | None,
) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert entities["domain"] == expected_domain
    assert entities.get("tax_concept") == expected_tax_concept
    assert entities.get("nationality") == expected_nationality


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_domain", "expected_entity"),
    [
        ("berapa pajak penghasilan", "tax", None),
        ("aturan perpajakan terbaru", "tax", None),
        ("pertanahan", "property", None),
        ("visa requirements", "visa", None),
        ("VAT rate", "tax", {"tax_concept": "PPN"}),
        ("I am from the USA", "general", {"nationality": "USA"}),
        ("a villa on the island", "property", None),
    ],
)
async def test_extract_entities_keeps_legitimate_keyword_matches(
    query: str, expected_domain: str, expected_entity: dict[str, str] | None
) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert entities["domain"] == expected_domain
    if expected_entity:
        assert entities.items() >= expected_entity.items()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_domain"),
    [
        ("what taxes does a foreigner pay", "tax"),
        ("taxation of dividends", "tax"),
        ("how many visas can one sponsor hold", "visa"),
        ("leasehold or freehold", "property"),
        ("who is the landlord", "property"),
    ],
)
async def test_extract_entities_keeps_plurals_and_derived_words(
    query: str, expected_domain: str
) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert entities["domain"] == expected_domain


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["56101", "requirements for 62019", "56101?"])
async def test_extract_entities_still_reads_a_code_written_alone(query: str) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert entities["kbli_code"] == query.strip("?").split()[-1]
    assert entities["domain"] == "kbli"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "I can invest 50000 usd",
        "the deposit is 25000 dollars",
        "about 15000 juta in assets",
        "a fee of 12500.50",
        "capital of idr 10000",
    ],
)
async def test_extract_entities_never_reads_an_amount_as_a_code(query: str) -> None:
    service = EntityExtractionService()

    entities = await service.extract_entities(query)

    assert "kbli_code" not in entities
