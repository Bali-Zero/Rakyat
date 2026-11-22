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
