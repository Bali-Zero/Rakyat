"""A scenario price counts each pricing source once (finding be-garuda-finance-75).

The cross-oracle synthesis already carries ``bali_zero_pricing_hybrid`` as one
of its sources. The pricing service used to query that collection a second
time and add the same chunks again, inflating the setup total.
"""

from unittest.mock import AsyncMock, create_autospec

import pytest

from backend.services.oracle.cross_oracle_synthesis_service import (
    CrossOracleSynthesisService,
    SynthesisResult,
)
from backend.services.pricing.dynamic_pricing_service import DynamicPricingService
from backend.services.search.search_service import SearchService

PRICING = "bali_zero_pricing_hybrid"
CHUNK_TEXT = "Contoh notary fee for incorporation: Rp 5.000.000 one time."


def _synthesis_result(sources: dict) -> SynthesisResult:
    return SynthesisResult(
        query="Contoh scenario",
        scenario_type="business_setup",
        oracles_consulted=[k for k, v in sources.items() if v["success"]],
        synthesis="Contoh synthesis",
        timeline=None,
        investment=None,
        key_requirements=[],
        risks=[],
        sources=sources,
        confidence=0.8,
    )


def _source(collection: str, results: list[dict]) -> dict:
    return {
        "collection": collection,
        "results": results,
        "result_count": len(results),
        "success": bool(results),
    }


def _service(sources: dict, direct_results: list[dict]):
    synthesis = create_autospec(CrossOracleSynthesisService, instance=True)
    synthesis.synthesize = AsyncMock(return_value=_synthesis_result(sources))
    search = create_autospec(SearchService, instance=True)
    search.search = AsyncMock(return_value={"results": direct_results})
    service = DynamicPricingService(cross_oracle_synthesis_service=synthesis, search_service=search)
    return service, search


@pytest.mark.asyncio
async def test_pricing_chunk_in_synthesis_is_not_counted_again_by_the_direct_query():
    chunk = {"id": "chunk-1", "text": CHUNK_TEXT}
    service, search = _service(
        {PRICING: _source(PRICING, [chunk])},
        direct_results=[chunk],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 5_000_000
    search.search.assert_not_called()


@pytest.mark.asyncio
async def test_same_chunk_arriving_through_two_sources_is_counted_once():
    chunk = {"id": "chunk-1", "text": CHUNK_TEXT}
    service, _ = _service(
        {
            "legal_architect": _source("legal_architect", [chunk]),
            PRICING: _source(PRICING, [chunk]),
        },
        direct_results=[],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 5_000_000
    assert len(result.cost_items) == 1


@pytest.mark.asyncio
async def test_same_text_without_ids_is_counted_once():
    service, _ = _service(
        {
            "legal_architect": _source("legal_architect", [{"text": CHUNK_TEXT}]),
            "tax_genius": _source("tax_genius", [{"text": "  " + CHUNK_TEXT.upper()}]),
        },
        direct_results=[],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 5_000_000


@pytest.mark.asyncio
async def test_synthesis_without_the_pricing_source_still_uses_the_direct_query():
    service, search = _service(
        {"legal_architect": _source("legal_architect", [])},
        direct_results=[{"id": "chunk-9", "text": CHUNK_TEXT}],
    )

    result = await service.calculate_pricing("Contoh scenario")

    search.search.assert_awaited_once()
    assert result.total_setup_cost == 5_000_000


@pytest.mark.asyncio
async def test_failed_pricing_source_in_synthesis_does_not_skip_the_direct_query():
    failed = {"collection": PRICING, "results": [], "result_count": 0, "success": False}
    service, search = _service({PRICING: failed}, direct_results=[{"id": "c", "text": CHUNK_TEXT}])

    result = await service.calculate_pricing("Contoh scenario")

    search.search.assert_awaited_once()
    assert result.total_setup_cost == 5_000_000


@pytest.mark.asyncio
async def test_different_chunks_with_the_same_amount_are_both_counted():
    service, _ = _service(
        {
            "legal_architect": _source(
                "legal_architect",
                [
                    {"id": "chunk-1", "text": "Contoh notary fee: Rp 5.000.000"},
                    {"id": "chunk-2", "text": "Contoh licence fee: Rp 5.000.000"},
                ],
            ),
        },
        direct_results=[],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 10_000_000


@pytest.mark.asyncio
async def test_two_different_lines_with_the_same_amount_in_one_chunk_are_both_counted():
    text = (
        "Contoh notary fee for the deed of establishment: Rp 5.000.000 one time. "
        "Contoh company registration and other documents handled by the team. "
        "Contoh licence fee for the business permit: Rp 5.000.000 one time."
    )
    service, _ = _service(
        {"legal_architect": _source("legal_architect", [{"id": "chunk-1", "text": text}])},
        direct_results=[],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 10_000_000


@pytest.mark.asyncio
async def test_two_equal_prices_in_one_short_chunk_are_both_counted():
    # Under ~100 characters the two context windows both span the whole chunk,
    # so the descriptions read the same: only the position tells them apart.
    text = "Contoh akta Rp 5.000.000 dan izin Rp 5.000.000"
    assert len(text) < 100
    chunk = {"id": "chunk-short", "text": text}
    service, _ = _service({PRICING: _source(PRICING, [chunk])}, direct_results=[])

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 10_000_000
    assert len(result.cost_items) == 2


@pytest.mark.asyncio
async def test_a_short_chunk_seen_twice_still_counts_its_prices_once_each():
    text = "Contoh akta Rp 5.000.000 dan izin Rp 5.000.000"
    chunk = {"id": "chunk-short", "text": text}
    service, _ = _service(
        {
            "legal_architect": _source("legal_architect", [chunk]),
            PRICING: _source(PRICING, [chunk]),
        },
        direct_results=[],
    )

    result = await service.calculate_pricing("Contoh scenario")

    assert result.total_setup_cost == 10_000_000
