"""Round-2 R1/R2: with an activation date set, the golden table routes fully.

All 110 golden codes route to their golden entity key from the effective WITA
calendar day onward (the 28 ``tax*`` codes to ``permata_bali_nol_konsultan``),
and every code falls back to the default entity strictly before that day.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import (
    DEFAULT_ENTITY_KEY,
    ENTITIES,
    InvoiceEntity,
    resolve_invoice_entity,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "invoicing"

EFFECTIVE = date(2027, 1, 1)
STRICTLY_BEFORE = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)  # WITA 2026-12-31
FIRST_WITA_INSTANT = datetime(2026, 12, 31, 16, 0, 0, tzinfo=timezone.utc)  # WITA 2027-01-01
LATER = datetime(2027, 6, 1, tzinfo=timezone.utc)


def _synthetic_entities() -> dict[str, InvoiceEntity]:
    base = {
        "tax_id": "00.000.000.0-000.000",
        "address": None,
        "bank_account_name": "Synthetic Holder One",
        "bank_account_number": "0000000001",
        "bank_name": "Synthetic Bank One",
        "bank_branch": "Synthetic Branch One",
        "bank_swift": "SYNTHIDJA",
    }
    a = InvoiceEntity(key="cimb_bayu_bali_nol", company_name="Synthetic Issuer One", **base)
    b = InvoiceEntity(
        key="permata_bali_nol_konsultan",
        company_name="Synthetic Issuer Two",
        **{
            **base,
            "tax_id": "11.111.111.1-111.111",
            "bank_account_number": "0000000002",
            "bank_branch": None,
            "bank_swift": None,
        },
    )
    c = InvoiceEntity(
        key="mandiri_bali_nol_impresariat",
        company_name="Synthetic Issuer Three",
        **{**base, "tax_id": "22.222.222.2-222.222", "bank_account_number": "0000000003"},
    )
    d = InvoiceEntity(
        key="bni_bayu_santero",
        company_name="Synthetic Issuer Four",
        **{**base, "tax_id": "33.333.333.3-333.333", "bank_account_number": "0000000004"},
    )
    return {e.key: e for e in (a, b, c, d)}


@pytest.fixture
def synthetic_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, InvoiceEntity]:
    registry = _synthetic_entities()
    monkeypatch.setattr(entities, "ENTITIES", registry)
    assert set(registry) == set(ENTITIES)
    assert all(key == entity.key for key, entity in registry.items())
    assert all(entity.is_complete for entity in registry.values())
    return registry


def _golden() -> dict[str, str]:
    return json.loads((FIXTURES / "invoice_code_entity_golden.json").read_text("utf-8"))


@pytest.mark.parametrize("instant", [FIRST_WITA_INSTANT, LATER])
def test_all_golden_codes_route_to_golden_entity_key(
    synthetic_registry: dict[str, InvoiceEntity],
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", EFFECTIVE)
    golden = _golden()
    assert len(golden) == 121
    for code, expected_key in golden.items():
        resolved = resolve_invoice_entity(code, instant)
        assert resolved.key == expected_key, code
        assert resolved is synthetic_registry[expected_key], code


def test_all_golden_codes_fall_back_before_effective_day(
    synthetic_registry: dict[str, InvoiceEntity], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", EFFECTIVE)
    for code, expected_key in _golden().items():
        if expected_key == DEFAULT_ENTITY_KEY:
            continue
        resolved = resolve_invoice_entity(code, STRICTLY_BEFORE)
        assert resolved is synthetic_registry[DEFAULT_ENTITY_KEY], code


def test_all_tax_codes_route_to_permata(
    synthetic_registry: dict[str, InvoiceEntity], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", EFFECTIVE)
    # The reviewer moved tax_close_pma_company to the Impresariat issuer.
    tax_codes = sorted(
        code for code in _golden() if code.startswith("tax") and code != "tax_close_pma_company"
    )
    assert len(tax_codes) == 27
    for code in tax_codes:
        resolved = resolve_invoice_entity(code, FIRST_WITA_INSTANT)
        assert resolved is synthetic_registry["permata_bali_nol_konsultan"], code
    closing = resolve_invoice_entity("tax_close_pma_company", FIRST_WITA_INSTANT)
    assert closing is synthetic_registry["mandiri_bali_nol_impresariat"]
