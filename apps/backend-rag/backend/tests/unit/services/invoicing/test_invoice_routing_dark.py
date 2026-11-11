"""Routing is disabled by default: every invoice keeps the default issuer."""

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.services.invoicing import entities
from backend.services.invoicing.entities import (
    BNI_BAYU_SANTERO,
    CIMB_BAYU_BALI_NOL,
    MANDIRI_BALI_NOL_IMPRESARIAT,
    resolve_invoice_entity,
)

WITA = timezone(timedelta(hours=8))
MANDIRI_CODE = "visa_c1_tourism"
BNI_CODE = "voa_extension"
INSTANTS = [
    datetime(2026, 11, 11, 23, 59, tzinfo=WITA),
    datetime(2026, 11, 12, 0, 0, tzinfo=WITA),
    datetime(2026, 11, 13, 9, 0, tzinfo=WITA),
    datetime(2027, 6, 1, 9, 0, tzinfo=WITA),
]


def test_production_constant_is_disabled():
    assert entities.ENTITY_ROUTING_EFFECTIVE_FROM is None


def test_other_issuers_are_complete_so_default_is_due_to_the_switch():
    assert MANDIRI_BALI_NOL_IMPRESARIAT.is_complete
    assert BNI_BAYU_SANTERO.is_complete


@pytest.mark.parametrize("code", [MANDIRI_CODE, BNI_CODE])
@pytest.mark.parametrize("when", INSTANTS)
def test_invoices_from_the_old_go_live_date_on_stay_on_default(code, when):
    assert resolve_invoice_entity(code, None, now=when) == CIMB_BAYU_BALI_NOL
    assert resolve_invoice_entity(code, when) == CIMB_BAYU_BALI_NOL


@pytest.mark.parametrize("when", INSTANTS)
def test_every_mapped_code_stays_on_default(when):
    codes = [c for codes in entities.ENTITY_CODES.values() for c in codes]
    assert len(codes) > 100
    for code in codes:
        assert resolve_invoice_entity(code, None, now=when) == CIMB_BAYU_BALI_NOL


def test_setting_a_date_activates_routing(monkeypatch):
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    before = datetime(2026, 11, 11, 23, 59, tzinfo=WITA)
    after = datetime(2026, 11, 12, 0, 0, tzinfo=WITA)
    assert resolve_invoice_entity(MANDIRI_CODE, None, now=before) == CIMB_BAYU_BALI_NOL
    assert resolve_invoice_entity(MANDIRI_CODE, None, now=after) == MANDIRI_BALI_NOL_IMPRESARIAT
    assert resolve_invoice_entity(BNI_CODE, None, now=after) == BNI_BAYU_SANTERO
