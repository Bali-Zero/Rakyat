"""The partner welcome e-mail lists exactly the services a client may see, at the price a client sees.

Guilt (origin/main): the e-mail walked the RAW catalogue as category -> name -> row, so
``internal: true`` rows were listed to an external partner, ``tax_accounting`` sub-blocks were
emitted as services named after the sub-block ("On request"), and the raw ``price`` was shown
instead of the client display price. Synthetic fixtures only.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.services.crm.partners import emails
from backend.services.pricing.pricing_service import PricingService, _iter_service_entries

LABEL_BASIC = "Monthly tax report, without LKPM & Annual"
SUB_BLOCKS = (
    "monthly_tax_basic",
    "monthly_tax_bundled",
    "annual_basic_packages",
    "annual_standalone",
)


def _synthetic_service() -> PricingService:
    svc = PricingService()
    svc.prices = {
        "services": {
            "kitas_permits": {
                "Plain Permit": {"name": "Plain Permit Display", "price": "5.000.000 IDR"},
                "Secret Internal Permit": {
                    "name": "Secret Display",
                    "price": "9.000.000 IDR",
                    "internal": True,
                },
                "No Name Permit": {"price": "6.000.000 IDR"},
            },
            "tax_accounting": {
                "monthly_tax_basic": {
                    "Tier A": {
                        "name": "Report A (without LKPM)",
                        "price": "",
                        "tier_range": ["IDR 1.000.000", "IDR 2.000.000"],
                    },
                },
                "monthly_tax_bundled": {
                    "Tier A": {"name": "Report A (including LKPM)", "price": "3.000.000 IDR"},
                },
                "annual_standalone": {
                    "Hidden Annual": {
                        "name": "Hidden Annual Display",
                        "price": "7.000.000 IDR",
                        "internal": True,
                    },
                },
            },
        }
    }
    svc.loaded = True
    return svc


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    monkeypatch.setattr(emails, "get_pricing_service", _synthetic_service)
    return emails._build_pricing_services()


def test_internal_row_is_absent(synthetic: list[dict[str, str]]) -> None:
    names = {s["name"] for s in synthetic}
    assert not names & {"Secret Internal Permit", "Secret Display"}
    assert not names & {"Hidden Annual", "Hidden Annual Display"}


def test_nested_rows_appear_by_catalogue_name_with_display_price(
    synthetic: list[dict[str, str]],
) -> None:
    by_name = {s["name"]: s["price_display"] for s in synthetic}
    assert by_name["Report A (without LKPM)"] == "Rp 1.000.000 – 2.000.000"
    assert by_name["Report A (including LKPM)"] == "3.000.000 IDR"
    assert "Tier A" not in by_name
    for block in SUB_BLOCKS:
        assert block not in by_name


def test_normal_row_keeps_its_price_and_nameless_row_falls_back_to_key(
    synthetic: list[dict[str, str]],
) -> None:
    assert {"name": "Plain Permit Display", "price_display": "5.000.000 IDR"} in synthetic
    assert {"name": "No Name Permit", "price_display": "6.000.000 IDR"} in synthetic
    assert len(synthetic) == 4


def test_real_catalogue_lists_no_internal_row_and_no_sub_block() -> None:
    svc = emails.get_pricing_service()
    if not getattr(svc, "loaded", False):
        pytest.skip("bundled catalogue not loadable offline")
    raw: Any = svc.get_all_prices()["services"]
    rows = _iter_service_entries(raw)

    def shown(n: str, e: dict[str, Any]) -> str:
        return e["name"] if isinstance(e.get("name"), str) and e["name"].strip() else n

    internal = {shown(n, e) for _c, n, e in rows if e.get("internal") is True}
    public = {shown(n, e) for _c, n, e in rows if e.get("internal") is not True}
    assert internal, "the bundled catalogue is expected to carry internal rows"
    names = {s["name"] for s in emails._build_pricing_services()}
    assert names == public - (internal - public)
    assert not (internal - public) & names
    for block in SUB_BLOCKS:
        assert block not in names


def test_real_catalogue_names_are_unique_and_distinguishable() -> None:
    svc = emails.get_pricing_service()
    if not getattr(svc, "loaded", False):
        pytest.skip("bundled catalogue not loadable offline")
    names = [s["name"] for s in emails._build_pricing_services()]
    assert len(names) == len(set(names)), "two services share a name"
    for tier in ("0 to 50",):
        hits = [n for n in names if tier in n]
        assert any("without LKPM" in n for n in hits)
        assert any("including LKPM" in n for n in hits)
    for bare in ("Tier 0-50", "Tier 50-100", "Tier 100-200", "Tier 200+"):
        assert bare not in names
