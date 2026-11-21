"""Deterministic PricingTool adapter tests: exact lookup and strict amounts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.services.visa_engine.models import PricingKey, VisaProductVersion
from backend.services.visa_engine.pricing_adapter import (
    build_price_quote,
    resolve_candidate_pricing,
)
from backend.tests.services.visa_engine.gold_harness import loader as gold_loader

_NOW = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)


def _product(pricing_key: PricingKey | None) -> VisaProductVersion:
    product = gold_loader.load_and_compile_rule_pack().source_pack.payload.products[0]
    return product.model_copy(update={"pricing_key": pricing_key})


class _Catalog:
    def __init__(
        self,
        *,
        loaded: bool,
        row: dict[str, Any] | None = None,
    ) -> None:
        self.loaded = loaded
        self._row = row
        self.lookups: list[str] = []

    def get_service_by_key(self, key: str) -> dict[str, Any] | None:
        self.lookups.append(key)
        return self._row

    def get_all_prices(self) -> dict[str, Any]:
        return {
            "version": "test-2026.1",
            "metadata": {"last_updated": "2026-05-06", "currency": "IDR"},
            "services": {"single_entry_visas": {}},
        }


def _assert_no_amount(payload: dict[str, object]) -> None:
    assert "amount" not in payload
    assert "price" not in payload


def test_missing_product_pricing_key_requires_contact_without_lookup() -> None:
    catalog = _Catalog(loaded=True)
    result = resolve_candidate_pricing(
        _product(None),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "CONTACT_REQUIRED"
    assert result.reason_code == "PRICING_KEY_NOT_CONFIGURED"
    assert catalog.lookups == []
    _assert_no_amount(result.as_json())


def test_unloaded_catalog_is_unknown_without_lookup() -> None:
    catalog = _Catalog(loaded=False)
    result = resolve_candidate_pricing(
        _product(PricingKey(category="single_entry_visas", item_key="C1 exact")),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "UNKNOWN"
    assert result.reason_code == "PRICING_CATALOG_UNAVAILABLE"
    assert catalog.lookups == []
    _assert_no_amount(result.as_json())


def test_catalog_read_failure_is_unknown_without_lookup() -> None:
    class _BrokenCatalog(_Catalog):
        def get_all_prices(self) -> dict[str, Any]:
            return {"error": "test-only outage"}

    catalog = _BrokenCatalog(loaded=True)
    result = resolve_candidate_pricing(
        _product(PricingKey(category="single_entry_visas", item_key="C1 exact")),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "UNKNOWN"
    assert result.reason_code == "PRICING_CATALOG_UNAVAILABLE"
    assert catalog.lookups == []
    _assert_no_amount(result.as_json())


@pytest.mark.parametrize(
    "row",
    [None, {"key": "C1 exact", "category": "wrong_category", "price": "test-only"}],
)
def test_missing_exact_item_or_category_requires_contact(row: dict[str, Any] | None) -> None:
    catalog = _Catalog(loaded=True, row=row)
    result = resolve_candidate_pricing(
        _product(PricingKey(category="single_entry_visas", item_key="C1 exact")),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "CONTACT_REQUIRED"
    assert result.reason_code == "PRICING_ROW_MISSING"
    assert catalog.lookups == ["C1 exact"]
    assert result.catalog_sha256 is not None
    assert result.row_sha256 is None
    _assert_no_amount(result.as_json())


def test_exact_row_emits_approved_all_inclusive_amount() -> None:
    catalog = _Catalog(
        loaded=True,
        row={
            "key": "C1 exact",
            "category": "single_entry_visas",
            "price": "2.300.000 IDR",
        },
    )
    result = resolve_candidate_pricing(
        _product(PricingKey(category="single_entry_visas", item_key="C1 exact")),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "AVAILABLE"
    assert result.reason_code == "PRICE_AVAILABLE"
    assert result.amount == 2_300_000
    assert result.catalog_version == "test-2026.1"
    assert result.catalog_last_updated is not None
    assert result.catalog_sha256 is not None
    assert result.row_sha256 is not None
    _assert_no_amount(result.as_json())
    product = _product(PricingKey(category="single_entry_visas", item_key="C1 exact"))
    decision_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    quote = build_price_quote(product, result, decision_id=decision_id)
    assert quote is not None
    assert quote.amount == 2_300_000
    assert quote.catalog_version == "test-2026.1"
    quote_again = build_price_quote(
        product,
        result,
        decision_id=decision_id,
    )
    assert quote_again is not None
    assert quote.quote_id == quote_again.quote_id


@pytest.mark.parametrize(
    "raw_price",
    [
        "Contact",
        "1.800.000 – 2.000.000 IDR",
        "USD 100",
        "2.3 million IDR",
        "IDR 2.300.000 IDR",
        "2.300,000 IDR",
        "-2.300.000 IDR",
        "9,007,199,254,740,992 IDR",
        None,
    ],
)
def test_non_exact_or_non_idr_price_requires_contact(raw_price: object) -> None:
    catalog = _Catalog(
        loaded=True,
        row={
            "key": "C1 exact",
            "category": "single_entry_visas",
            "price": raw_price,
        },
    )
    result = resolve_candidate_pricing(
        _product(PricingKey(category="single_entry_visas", item_key="C1 exact")),
        pricing_catalog=catalog,
        evaluated_at=_NOW,
    )
    assert result.status == "CONTACT_REQUIRED"
    assert result.reason_code == "PRICING_ROW_NOT_EXACT_AMOUNT"
    assert result.amount is None
    assert result.row_sha256 is not None


@pytest.mark.parametrize(
    ("product_code", "stay_days", "offshore_amount", "onshore_amount", "onshore_item_key"),
    [
        # P01 / P06: E33F senior 1y income-only splits 14M Offshore vs 16M Altus/Onshore.
        (
            "E33F",
            365,
            14_000_000,
            16_000_000,
            "E33F Second Home Senior (1 Year, Altus/Onshore)",
        ),
        # P11: E28A Investor KITAS 2y splits 17M Offshore vs 19M Altus/Onshore.
        (
            "E28A",
            730,
            17_000_000,
            19_000_000,
            "Investor KITAS 2 Years (Altus/Onshore)",
        ),
        # P23: E31A Spouse KITAS 2y splits 15M Offshore vs 18M Altus/Onshore.
        (
            "E31A",
            730,
            15_000_000,
            18_000_000,
            "Spouse 2 Years (Altus/Onshore)",
        ),
        # E31A 1y, E31B/E31E/E31H/E31J 1y & 2y, E23, E33G.
        ("E31A", 365, 11_000_000, 13_500_000, "Spouse 1 Year (Altus/Onshore)"),
        ("E31B", 365, 11_000_000, 13_500_000, "Dependent 1 Year (Altus/Onshore)"),
        ("E31E", 730, 15_000_000, 18_000_000, "Dependent 2 Years (Altus/Onshore)"),
        ("E31H", 365, 11_000_000, 13_500_000, "Dependent 1 Year (Altus/Onshore)"),
        ("E31J", 730, 15_000_000, 18_000_000, "Dependent 2 Years (Altus/Onshore)"),
        ("E23", 365, 34_500_000, 36_500_000, "Working KITAS (Altus/Onshore)"),
        ("E33G", 365, 13_000_000, 14_000_000, "E33G Remote Worker (Altus/Onshore)"),
        # Products without a separate onshore SKU keep their base pricing_key unchanged.
        ("E33", 1825, 35_000_000, 35_000_000, "E33 Second Home (5 Years)"),
        ("E33E", 1825, 45_000_000, 45_000_000, "E33E Second Home Senior (5 Years)"),
    ],
)
def test_onshore_conversion_resolves_altus_onshore_sku_when_catalogued(
    product_code: str,
    stay_days: int,
    offshore_amount: int,
    onshore_amount: int,
    onshore_item_key: str,
) -> None:
    from backend.scripts.visa_engine.gold_replay_driver import (
        PACKS_DIR,
        _parse_utc,
        _repository_trust_store,
        select_highest_repository_pack,
    )
    from backend.services.pricing.pricing_service import get_pricing_service
    from backend.services.visa_engine.bundle import verify_rule_pack
    from backend.services.visa_engine.evaluate_path import _duration_display
    from backend.services.visa_engine.pricing_adapter import effective_pricing_key

    _pack_path, envelope = select_highest_repository_pack(PACKS_DIR)
    verified = verify_rule_pack(
        envelope,
        trust_store=_repository_trust_store(),
        observed_at=_parse_utc(envelope["protected"]["signed_at"]),
    )
    product = next(
        p for p in verified.pack.payload.products if p.product_code == product_code
    )
    catalog = get_pricing_service()
    decision_id = uuid.UUID("00000000-0000-4000-8000-000000000099")

    offshore_res = resolve_candidate_pricing(
        product,
        pricing_catalog=catalog,
        evaluated_at=_NOW,
        stay_days=stay_days,
        application_channel="OFFSHORE",
    )
    default_res = resolve_candidate_pricing(
        product,
        pricing_catalog=catalog,
        evaluated_at=_NOW,
        stay_days=stay_days,
    )
    onshore_res = resolve_candidate_pricing(
        product,
        pricing_catalog=catalog,
        evaluated_at=_NOW,
        stay_days=stay_days,
        application_channel="ONSHORE_CONVERSION",
    )

    assert offshore_res.status == "AVAILABLE"
    assert default_res.amount == offshore_amount
    assert offshore_res.amount == offshore_amount
    assert onshore_res.status == "AVAILABLE"
    assert onshore_res.amount == onshore_amount

    eff_key = effective_pricing_key(
        product,
        stay_days,
        application_channel="ONSHORE_CONVERSION",
        pricing_catalog=catalog,
    )
    assert eff_key is not None
    assert eff_key.item_key == onshore_item_key

    onshore_quote = build_price_quote(
        product,
        onshore_res,
        decision_id=decision_id,
        stay_days=stay_days,
        application_channel="ONSHORE_CONVERSION",
    )
    assert onshore_quote is not None
    assert onshore_quote.amount == onshore_amount
    assert onshore_quote.pricing_key.item_key == onshore_item_key

    if product.duration_options:
        shown = _duration_display(
            product,
            stay_days=stay_days,
            pricing_catalog=catalog,
            evaluated_at=_NOW,
            application_channel="ONSHORE_CONVERSION",
        )
        selected = next(opt for opt in shown["duration_options"] if opt["selected"])  # type: ignore[index,union-attr]
        assert selected["pricing_key"]["item_key"] == onshore_item_key  # type: ignore[index]
        assert selected["amount_idr"] == onshore_amount  # type: ignore[index]


def test_onshore_conversion_falls_back_to_base_key_when_no_onshore_row_in_catalog() -> None:
    """When a custom/partial catalog has no Altus/Onshore sibling row, keep the base key."""

    class _OffshoreOnlyCatalog(_Catalog):
        def get_service_by_key(self, key: str) -> dict[str, Any] | None:
            self.lookups.append(key)
            if key == "Custom Permit (Offshore)":
                return {
                    "key": key,
                    "category": "kitas_permits",
                    "price": "12.000.000 IDR",
                }
            return None

    catalog = _OffshoreOnlyCatalog(loaded=True)
    product = _product(PricingKey(category="kitas_permits", item_key="Custom Permit (Offshore)"))
    res = resolve_candidate_pricing(
        product,
        pricing_catalog=catalog,
        evaluated_at=_NOW,
        application_channel="ONSHORE_CONVERSION",
    )
    assert res.status == "AVAILABLE"
    assert res.amount == 12_000_000
    assert res.pricing_key == PricingKey(
        category="kitas_permits", item_key="Custom Permit (Offshore)"
    )

    # Without a catalog instance, resolve_channel_pricing_key and build_price_quote
    # keep the verified base key rather than synthesising an unverified sibling SKU.
    from datetime import date

    from backend.services.visa_engine.pricing_adapter import (
        PricingResolution,
        resolve_channel_pricing_key,
    )

    base_key = PricingKey(category="kitas_permits", item_key="Custom Permit (Offshore)")
    assert (
        resolve_channel_pricing_key(
            base_key, application_channel="ONSHORE_CONVERSION", pricing_catalog=None
        )
        == base_key
    )
    manual_res = PricingResolution(
        status="AVAILABLE",
        reason_code="PRICE_AVAILABLE",
        evaluated_at=_NOW,
        catalog_last_updated=date(2026, 4, 21),
        catalog_version="2026.1",
        catalog_sha256="a" * 64,
        row_sha256="b" * 64,
        amount=12_000_000,
    )
    quote_without_catalog = build_price_quote(
        product,
        manual_res,
        decision_id=uuid.UUID("00000000-0000-4000-8000-000000000099"),
        application_channel="ONSHORE_CONVERSION",
    )
    assert quote_without_catalog is not None
    assert quote_without_catalog.pricing_key == base_key
