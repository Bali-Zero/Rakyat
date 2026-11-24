"""Production routing is ACTIVE from the first WITA day 2026-11-11 (owner order).

These tests read the production constant and the real registry; they compare keys and
completeness only, never any company value.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, resolve_invoice_entity

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "invoicing"
ACTIVATION_UTC = datetime(2026, 11, 10, 16, 0, 0, tzinfo=timezone.utc)  # 00:00 WITA 2026-11-11
ONE_SECOND_BEFORE = datetime(2026, 11, 10, 15, 59, 59, tzinfo=timezone.utc)
LATER = datetime(2026, 12, 5, 3, 0, 0, tzinfo=timezone.utc)


def _golden() -> dict[str, str]:
    return json.loads((FIXTURES / "invoice_code_entity_golden.json").read_text("utf-8"))


def test_production_constant_is_the_ordered_date() -> None:
    assert entities.ENTITY_ROUTING_EFFECTIVE_FROM == date(2026, 11, 11)
    fixture = json.loads((FIXTURES / "invoice_routing_effective_date.json").read_text("utf-8"))
    assert fixture["effective_from"] == "2026-11-11"


def test_all_four_issuers_are_complete_in_production() -> None:
    assert sorted(entities.ENTITIES) == [
        "bni_bayu_santero",
        "cimb_bayu_bali_nol",
        "mandiri_bali_nol_impresariat",
        "permata_bali_nol_konsultan",
    ]
    for key, entity in entities.ENTITIES.items():
        assert entity.key == key
        assert entity.missing_fields() == [], key


@pytest.mark.parametrize("instant", [ACTIVATION_UTC, LATER], ids=["at_activation", "later"])
def test_every_golden_code_routes_to_its_golden_issuer(instant: datetime) -> None:
    golden = _golden()
    assert len(golden) == 123
    for code, key in golden.items():
        assert resolve_invoice_entity(code, instant).key == key, code


def test_one_second_before_activation_everything_is_still_default() -> None:
    for code in _golden():
        assert resolve_invoice_entity(code, ONE_SECOND_BEFORE).key == DEFAULT_ENTITY_KEY, code


def test_each_issuer_is_reached_by_at_least_one_code() -> None:
    reached = {resolve_invoice_entity(code, LATER).key for code in _golden()}
    assert reached == set(entities.ENTITIES)
