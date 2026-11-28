"""Round-2 R2: the dark switch (``ENTITY_ROUTING_EFFECTIVE_FROM = None``) still works.

Production is activated (see test_invoice_routing_live_activation.py); these tests
pin the OFF state, so they set the switch to ``None`` explicitly. With it ``None``,
every practice type code — mapped in the golden table or not — resolves to the
default entity at any instant, while unmapped codes stay loud (warning +
counter). Strict router types are enforced regardless of the switch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from backend.services.invoicing import entities
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, resolve_invoice_entity

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "invoicing"
_COUNTER_NAME = "zantara_invoice_issuer_unmapped_total"

INSTANTS = [
    datetime(2026, 11, 11, tzinfo=timezone.utc),
    datetime(2027, 1, 2, tzinfo=timezone.utc),
    datetime(2030, 6, 30, 12, 0, 0, tzinfo=timezone.utc),
]

REMOVED_CODES = [
    "merp_1yr",
    "merp_2yr",
    "other_acc_mutation",
    "other_acc_mutation_passport",
    "other_cancel_rptka_imta_wl",
    "other_domicilie",
    "other_domicilie_sktt",
    "visa_voa",
    "voa_extension",
]

NON_DEFAULT_ISSUER_CODES = [
    "tax",
    "tax_monthly_basic_0_50",
    "tax_consulting",
    "visa_c1_tourism",
    "visa_b1_voa",
    "ext_b1_voa",
    "ext_c1_tourism",
]


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", None)


def _golden() -> dict[str, str]:
    return json.loads((FIXTURES / "invoice_code_entity_golden.json").read_text("utf-8"))


@pytest.mark.parametrize("instant", INSTANTS, ids=[str(i.date()) for i in INSTANTS])
def test_every_golden_code_resolves_to_default_entity(instant: datetime) -> None:
    assert entities.ENTITY_ROUTING_EFFECTIVE_FROM is None
    golden = _golden()
    assert len(golden) == 153
    for code in golden:
        assert resolve_invoice_entity(code, instant).key == DEFAULT_ENTITY_KEY, code


def test_non_default_issuer_codes_resolve_to_default_entity() -> None:
    assert entities.ENTITY_ROUTING_EFFECTIVE_FROM is None
    instant = datetime(2027, 1, 2, tzinfo=timezone.utc)
    for code in NON_DEFAULT_ISSUER_CODES:
        assert resolve_invoice_entity(code, instant).key == DEFAULT_ENTITY_KEY, code


def test_removed_codes_stay_loud_unmapped_while_dark(caplog: pytest.LogCaptureFixture) -> None:
    labels = {"practice_type_code": "__other__", "code_kind": "unknown"}
    before = REGISTRY.get_sample_value(_COUNTER_NAME, labels) or 0.0
    instant = datetime(2027, 1, 2, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="backend.services.invoicing.entities"):
        for code in REMOVED_CODES:
            resolved = resolve_invoice_entity(code, instant)
            assert resolved.key == DEFAULT_ENTITY_KEY, code
    assert REGISTRY.get_sample_value(_COUNTER_NAME, labels) - before == len(REMOVED_CODES)
    warnings = [r for r in caplog.records if r.name == "backend.services.invoicing.entities"]
    assert len(warnings) == len(REMOVED_CODES)


def test_strict_types_enforced_while_dark() -> None:
    assert entities.ENTITY_ROUTING_EFFECTIVE_FROM is None
    aware = datetime(2027, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        resolve_invoice_entity("kitas", datetime(2027, 1, 2, 0, 0, 0))
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", "2027-01-02T00:00:00Z")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity(123, aware)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity(b"kitas", aware)  # type: ignore[arg-type]
