"""A price search returns both rows that share a name.

The catalogue holds the monthly tax tiers twice under one name: in ``monthly_tax_basic`` (a
price range, LKPM and annual tax not included) and in ``monthly_tax_bundled`` (one price, both
included). The search keyed what it found by the bare name, so the second row replaced the
first: a question about a tier came back with the bundled price only, as if it were the price
of that tier. ``get_service_by_key`` already refuses the bare name and reads a qualified one;
the search now returns rows that share a name under that qualified key.

The colliding names are read from the shipped catalogue, never typed here, and no price is
pinned: a price change must not turn this file red.
"""

from __future__ import annotations

import collections
from typing import Any

import pytest

from backend.app.routers.dynamic_pricing import search_pricing
from backend.services.pricing import pricing_service as pricing_module
from backend.services.pricing.pricing_service import (
    _KEY_QUALIFIER_SEP,
    _NESTED_CATEGORIES,
    PricingService,
)
from backend.services.rag.agentic.wa_package_builder import _sanitize_pricing_block


@pytest.fixture(scope="module")
def svc() -> PricingService:
    service = PricingService()
    assert service.loaded
    return service


def _shared_names(service: PricingService) -> dict[str, dict[str, dict[str, Any]]]:
    """name -> {sub_block: row} for every name two sub-blocks of a nested category hold."""
    found: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for category in _NESTED_CATEGORIES:
        for sub_block, rows in service.prices["services"][category].items():
            if isinstance(rows, dict):
                for name, row in rows.items():
                    if isinstance(row, dict):
                        found[name][sub_block] = row
    return {name: blocks for name, blocks in found.items() if len(blocks) > 1}


_SHARED = sorted(_shared_names(PricingService()))


def test_the_catalogue_still_holds_names_that_two_sub_blocks_share() -> None:
    # Without one, every case below would pass on nothing.
    assert _SHARED


@pytest.mark.parametrize("name", _SHARED)
def test_a_search_for_a_shared_name_returns_the_row_of_every_sub_block(
    svc: PricingService, name: str
) -> None:
    blocks = _shared_names(svc)[name]

    rows = svc.search_service(name)["results"]["tax_accounting"]

    for sub_block, catalogue_row in blocks.items():
        returned = rows[f"{sub_block}{_KEY_QUALIFIER_SEP}{name}"]
        assert returned["_sub_block"] == sub_block
        assert {k: v for k, v in returned.items() if k != "_sub_block"} == catalogue_row
    assert name not in rows  # the bare name would say one price is THE price


@pytest.mark.parametrize("name", _SHARED)
def test_every_key_a_search_returns_is_one_the_exact_lookup_resolves(
    svc: PricingService, name: str
) -> None:
    results = svc.search_service(name)["results"]

    for category, rows in results.items():
        for key in rows:
            exact = svc.get_service_by_key(key)
            assert exact is not None, f"{category}: {key!r} does not resolve"
            assert exact["key"] == key


@pytest.mark.parametrize("name", _SHARED)
def test_the_two_rows_of_a_shared_name_carry_their_own_prices(
    svc: PricingService, name: str
) -> None:
    rows = svc.search_service(name)["results"]["tax_accounting"]

    shown = {
        key: (row.get("price"), tuple(row.get("tier_range") or ())) for key, row in rows.items()
    }
    qualified = [key for key in shown if key.endswith(f"{_KEY_QUALIFIER_SEP}{name}")]
    assert len(qualified) == 2
    assert shown[qualified[0]] != shown[qualified[1]]


def _synthetic(monkeypatch: pytest.MonkeyPatch) -> PricingService:
    """A catalogue of three invented rows: one name in two sub-blocks, one name alone."""
    service = PricingService()
    monkeypatch.setattr(
        service,
        "prices",
        {
            "services": {
                "tax_accounting": {
                    "zz_plain": {
                        "Zz Plan": {"name": "Zz plan alone", "price": "1 IDR", "notes": "walnut"},
                        "Zz Solo": {"name": "Zz solo row", "price": "3 IDR", "notes": ""},
                    },
                    "zz_bundled": {
                        "Zz Plan": {"name": "Zz plan bundled", "price": "2 IDR", "notes": "cedar"},
                    },
                }
            }
        },
    )
    monkeypatch.setattr(service, "loaded", True)
    return service


def test_rows_that_share_a_name_are_qualified_and_a_row_alone_keeps_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _synthetic(monkeypatch).search_service("zz")["results"]["tax_accounting"]

    assert {key: row["price"] for key, row in rows.items()} == {
        f"zz_plain{_KEY_QUALIFIER_SEP}Zz Plan": "1 IDR",
        f"zz_bundled{_KEY_QUALIFIER_SEP}Zz Plan": "2 IDR",
        "Zz Solo": "3 IDR",
    }


def test_a_shared_name_matched_in_one_sub_block_only_keeps_its_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only one row answers this question, so there is nothing to tell apart. As before.
    rows = _synthetic(monkeypatch).search_service("walnut")["results"]["tax_accounting"]

    assert list(rows) == ["Zz Plan"]
    assert rows["Zz Plan"]["price"] == "1 IDR"
    assert rows["Zz Plan"]["_sub_block"] == "zz_plain"


# ── what the consumers of the search do with two rows ────────────────────────


@pytest.mark.parametrize("name", _SHARED)
def test_the_whatsapp_package_keeps_both_rows(svc: PricingService, name: str) -> None:
    block = _sanitize_pricing_block(svc.search_service(name))

    assert block is not None
    kept = [key for key in block["results"]["tax_accounting"] if key.endswith(name)]
    assert len(kept) == 2
    for key in kept:
        assert "_sub_block" not in block["results"]["tax_accounting"][key]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _SHARED)
async def test_the_public_search_route_answers_with_both_rows(
    svc: PricingService, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pricing_module, "_pricing_service", svc, raising=False)

    answer = await search_pricing(q=name)

    keys = [key for key in answer["results"]["tax_accounting"] if key.endswith(name)]
    assert len(keys) == 2
