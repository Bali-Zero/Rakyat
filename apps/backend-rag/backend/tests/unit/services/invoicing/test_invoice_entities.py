"""Section-6 spec tests for the invoice issuer entity registry and router.

All expected values come from the conductor-supplied fixtures under
``backend/tests/fixtures/invoicing/`` or from literals in the spec; the
production mapping/date objects are only ever the actual value under test.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

from backend.services.invoicing import entities
from backend.services.invoicing.entities import (
    DEFAULT_ENTITY_KEY,
    ENTITIES,
    ENTITY_ROUTING_EFFECTIVE_FROM,
    KNOWN_UNMAPPED_CODES,
    PRACTICE_TYPE_ENTITY_KEYS,
    InvoiceEntity,
    entity_key_for_code,
    resolve_invoice_entity,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "invoicing"
ENTITY_KEYS = frozenset(ENTITIES)
_COUNTER_NAME = "zantara_invoice_issuer_unmapped_total"
_KNOWN_UNMAPPED_FROM_SPEC = ["other_acc_boarding_pass"]


def _load_json_no_duplicate_keys(path: Path) -> dict[str, Any]:
    def _hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"duplicate JSON key {key!r} in {path.name}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_hook)


def _synthetic_entities() -> dict[str, InvoiceEntity]:
    a = InvoiceEntity(
        key="cimb_bayu_bali_nol",
        company_name="Synthetic Issuer One",
        tax_id="00.000.000.0-000.000",
        address=None,
        bank_account_name="Synthetic Holder One",
        bank_account_number="0000000001",
        bank_name="Synthetic Bank One",
        bank_branch="Synthetic Branch One",
        bank_swift="SYNTHIDJA",
    )
    b = InvoiceEntity(
        key="mandiri_bali_nol_impresariat",
        company_name="Synthetic Issuer Two",
        tax_id="11.111.111.1-111.111",
        address="Synthetic Street Two 2, Synthetic City",
        bank_account_name="Synthetic Holder Two",
        bank_account_number="0000000002",
        bank_name="Synthetic Bank Two",
        bank_branch="Synthetic Branch Two",
        bank_swift="SYNTHIDJB",
    )
    c = InvoiceEntity(
        key="bni_bayu_santero",
        company_name="Synthetic Issuer Three",
        tax_id="22.222.222.2-222.222",
        address="Synthetic Street Three 3, Synthetic City",
        bank_account_name="Synthetic Holder Three",
        bank_account_number="0000000003",
        bank_name="Synthetic Bank Three",
        bank_branch="Synthetic Branch Three",
        bank_swift="SYNTHIDJC",
    )
    d = InvoiceEntity(
        key="permata_bali_nol_konsultan",
        company_name="Synthetic Issuer Four",
        tax_id="33.333.333.3-333.333",
        address="Synthetic Street Four 4, Synthetic City",
        bank_account_name="Synthetic Holder Four",
        bank_account_number="0000000004",
        bank_name="Synthetic Bank Four",
        bank_branch=None,
        bank_swift=None,
    )
    return {e.key: e for e in (a, b, c, d)}


@pytest.fixture
def synthetic_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, InvoiceEntity]:
    registry = _synthetic_entities()
    monkeypatch.setattr(entities, "ENTITIES", registry)
    assert set(registry) == ENTITY_KEYS
    assert all(key == entity.key for key, entity in registry.items())
    assert all(entity.is_complete for entity in registry.values())
    return registry


def _counter_value(labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(_COUNTER_NAME, labels) or 0.0


class _Utc(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


class _NullOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None


# ============================================================
# Mapping contract (independent golden fixture)
# ============================================================


def test_mapping_matches_independent_golden() -> None:
    golden = _load_json_no_duplicate_keys(FIXTURES / "invoice_code_entity_golden.json")
    assert golden == PRACTICE_TYPE_ENTITY_KEYS
    assert len(golden) == 153
    assert len(PRACTICE_TYPE_ENTITY_KEYS) == 153
    counts = Counter(golden.values())
    assert counts["cimb_bayu_bali_nol"] == 44
    assert counts["permata_bali_nol_konsultan"] == 52
    assert counts["mandiri_bali_nol_impresariat"] == 53
    assert counts["bni_bayu_santero"] == 4
    assert all(code == code.strip().lower() for code in golden), "golden keys must be normalized"
    assert set(golden.values()) <= ENTITY_KEYS, "golden values must be recognized entity keys"


def test_all_golden_codes_route_after_effective_date(
    synthetic_registry: dict[str, InvoiceEntity], monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _load_json_no_duplicate_keys(FIXTURES / "invoice_code_entity_golden.json")
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2027, 1, 1))
    fixed_instant = datetime(2027, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    for code, expected_key in golden.items():
        resolved = resolve_invoice_entity(code, fixed_instant)
        assert resolved.key == expected_key, code
        assert resolved is synthetic_registry[expected_key], code


def test_known_unmapped_inventory_matches_spec() -> None:
    assert sorted(KNOWN_UNMAPPED_CODES) == sorted(_KNOWN_UNMAPPED_FROM_SPEC)


# ============================================================
# Effective-date contract
# ============================================================


def test_effective_date_matches_approved_fixture() -> None:
    fixture = json.loads((FIXTURES / "invoice_routing_effective_date.json").read_text("utf-8"))
    raw = fixture["effective_from"]
    if raw is None:
        assert ENTITY_ROUTING_EFFECTIVE_FROM is None
    else:
        assert date.fromisoformat(raw) == ENTITY_ROUTING_EFFECTIVE_FROM


def test_literal_utc_cutover(
    synthetic_registry: dict[str, InvoiceEntity], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    before = datetime(2026, 11, 11, 15, 59, 59, tzinfo=timezone.utc)
    at_cutover = datetime(2026, 11, 11, 16, 0, 0, tzinfo=timezone.utc)
    on_date_end = datetime(2026, 11, 12, 15, 59, 59, tzinfo=timezone.utc)
    assert (
        resolve_invoice_entity("kitas_working_onshore", before)
        is synthetic_registry["cimb_bayu_bali_nol"]
    )
    assert (
        resolve_invoice_entity("kitas_working_onshore", at_cutover)
        is synthetic_registry["mandiri_bali_nol_impresariat"]
    )
    # The effective date itself is inclusive (WITA calendar day 2026-11-12).
    assert (
        resolve_invoice_entity("kitas_working_onshore", on_date_end)
        is synthetic_registry["mandiri_bali_nol_impresariat"]
    )


def test_effective_date_is_configurable_data(
    synthetic_registry: dict[str, InvoiceEntity], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2030, 1, 1))
    before = datetime(2029, 12, 31, 15, 59, 59, tzinfo=timezone.utc)
    after = datetime(2029, 12, 31, 16, 0, 0, tzinfo=timezone.utc)
    assert (
        resolve_invoice_entity("kitas_working_onshore", before)
        is synthetic_registry["cimb_bayu_bali_nol"]
    )
    assert (
        resolve_invoice_entity("kitas_working_onshore", after)
        is synthetic_registry["mandiri_bali_nol_impresariat"]
    )


# ============================================================
# Strict router types
# ============================================================


def test_router_rejects_naive_datetime(synthetic_registry: dict[str, InvoiceEntity]) -> None:
    with pytest.raises(ValueError):
        resolve_invoice_entity("kitas", datetime(2026, 11, 12, 0, 0, 0))
    with pytest.raises(ValueError):
        resolve_invoice_entity("kitas", datetime(2026, 11, 12, 0, 0, 0, tzinfo=_NullOffset()))


def test_router_rejects_non_datetime(synthetic_registry: dict[str, InvoiceEntity]) -> None:
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", "2026-11-12T00:00:00Z")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", date(2026, 11, 12))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", 1731369600)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_invoice_entity("kitas", None)  # type: ignore[arg-type]


def test_router_rejects_non_string_code(synthetic_registry: dict[str, InvoiceEntity]) -> None:
    aware = datetime(2026, 11, 1, tzinfo=_Utc())
    for bad_code in (123, b"kitas", {"kitas": 1}, object()):  # type: ignore[assignment]
        with pytest.raises(TypeError):
            resolve_invoice_entity(bad_code, aware)  # type: ignore[arg-type]


# ============================================================
# Unmapped-code observability
# ============================================================


def _unmapped_cases() -> list[tuple[str, Any, str, str, str | None]]:
    cases = [
        (f"known_{code}", code, code, "known_unmapped", code) for code in _KNOWN_UNMAPPED_FROM_SPEC
    ]
    cases.append(
        ("novel_code", "synthetic_novel_code", "__other__", "unknown", "synthetic_novel_code")
    )
    cases.append(("none", None, "__none__", "none", None))
    cases.append(("empty", "", "__empty__", "empty", None))
    cases.append(("whitespace", "   ", "__empty__", "empty", None))
    cases.append(
        (
            "normalized_known",
            "  OTHER_ACC_BOARDING_PASS ",
            "other_acc_boarding_pass",
            "known_unmapped",
            "other_acc_boarding_pass",
        )
    )
    return cases


@pytest.mark.parametrize("case", _unmapped_cases(), ids=[c[0] for c in _unmapped_cases()])
def test_unmapped_warns_and_counts_once(
    synthetic_registry: dict[str, InvoiceEntity],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, Any, str, str, str | None],
) -> None:
    _, raw_code, metric_code, code_kind, log_code = case
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    labels = {"practice_type_code": metric_code, "code_kind": code_kind}
    before = _counter_value(labels)
    with caplog.at_level(logging.WARNING, logger="backend.services.invoicing.entities"):
        resolved = resolve_invoice_entity(raw_code, datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert resolved is synthetic_registry["cimb_bayu_bali_nol"]
    assert _counter_value(labels) - before == 1
    warnings = [r for r in caplog.records if r.name == "backend.services.invoicing.entities"]
    assert len(warnings) == 1, [r.msg for r in caplog.records]
    record = warnings[0]
    assert record.levelno == logging.WARNING
    context = record.context  # type: ignore[attr-defined]
    assert context["event"] == "invoice_issuer_unmapped"
    assert context["code_kind"] == code_kind
    assert context["fallback_entity_key"] == DEFAULT_ENTITY_KEY
    assert context["counter"] == _COUNTER_NAME
    assert context["counter_increment"] == 1
    if log_code is None:
        assert context["practice_type_code"] in ("__none__", "__empty__")
    else:
        assert context["practice_type_code"] == log_code
        assert log_code in record.getMessage()
    assert DEFAULT_ENTITY_KEY in record.getMessage()


def test_unmapped_observable_before_effective_date(
    synthetic_registry: dict[str, InvoiceEntity],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    labels = {"practice_type_code": "other_acc_boarding_pass", "code_kind": "known_unmapped"}
    before = _counter_value(labels)
    with caplog.at_level(logging.WARNING, logger="backend.services.invoicing.entities"):
        resolved = resolve_invoice_entity(
            "other_acc_boarding_pass", datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
    assert resolved is synthetic_registry["cimb_bayu_bali_nol"]
    assert _counter_value(labels) - before == 1
    assert len(caplog.records) == 1


def test_mapped_a_does_not_warn(
    synthetic_registry: dict[str, InvoiceEntity],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    labels_none = {"practice_type_code": "__none__", "code_kind": "none"}
    before = _counter_value(labels_none)
    with caplog.at_level(logging.WARNING, logger="backend.services.invoicing.entities"):
        resolved = resolve_invoice_entity(
            " VISA_B1_VOA ", datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
    assert resolved is synthetic_registry["cimb_bayu_bali_nol"]
    assert _counter_value(labels_none) - before == 0
    assert [r for r in caplog.records if r.name == "backend.services.invoicing.entities"] == []


# ============================================================
# New-entity fallback behavior
# ============================================================


def test_incomplete_new_entity_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _synthetic_entities()
    incomplete_b = InvoiceEntity(
        **{**registry["mandiri_bali_nol_impresariat"].__dict__, "bank_name": " "}
    )
    registry["mandiri_bali_nol_impresariat"] = incomplete_b
    monkeypatch.setattr(entities, "ENTITIES", registry)
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    instant = datetime(2026, 11, 20, tzinfo=timezone.utc)
    resolved = resolve_invoice_entity("kitas_working_onshore", instant)
    assert resolved is registry["cimb_bayu_bali_nol"]


def test_incomplete_new_entity_logs_fallback_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _synthetic_entities()
    incomplete_b = InvoiceEntity(
        **{**registry["mandiri_bali_nol_impresariat"].__dict__, "tax_id": ""}
    )
    registry["mandiri_bali_nol_impresariat"] = incomplete_b
    monkeypatch.setattr(entities, "ENTITIES", registry)
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    with caplog.at_level(logging.ERROR, logger="backend.services.invoicing.entities"):
        resolved = resolve_invoice_entity(
            "kitas_working_onshore", datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
    assert resolved is registry["cimb_bayu_bali_nol"]
    errors = [r for r in caplog.records if r.name == "backend.services.invoicing.entities"]
    assert len(errors) == 1
    context = errors[0].context  # type: ignore[attr-defined]
    assert context["event"] == "invoice_issuer_fallback_default"
    assert context["reason"] == "mapped_entity_incomplete"
    assert context["configured_entity_key"] == "mandiri_bali_nol_impresariat"
    assert context["fallback_entity_key"] == "cimb_bayu_bali_nol"
    assert context["missing_fields"] == ["tax_id"]


def test_missing_mapped_entity_falls_back_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _synthetic_entities()
    del registry["mandiri_bali_nol_impresariat"]
    monkeypatch.setattr(entities, "ENTITIES", registry)
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    with caplog.at_level(logging.ERROR, logger="backend.services.invoicing.entities"):
        resolved = resolve_invoice_entity(
            "kitas_working_onshore", datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
    assert resolved is registry["cimb_bayu_bali_nol"]
    errors = [r for r in caplog.records if r.name == "backend.services.invoicing.entities"]
    assert len(errors) == 1
    assert errors[0].context["reason"] == "mapped_entity_missing"  # type: ignore[attr-defined]


def test_invalid_default_entity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _synthetic_entities()
    incomplete_a = InvoiceEntity(**{**registry["cimb_bayu_bali_nol"].__dict__, "tax_id": ""})
    registry["cimb_bayu_bali_nol"] = incomplete_a
    monkeypatch.setattr(entities, "ENTITIES", registry)
    monkeypatch.setattr(entities, "ENTITY_ROUTING_EFFECTIVE_FROM", date(2026, 11, 12))
    with pytest.raises(ValueError):
        resolve_invoice_entity("kitas", datetime(2026, 11, 20, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        resolve_invoice_entity("kitas_working_onshore", datetime(2026, 11, 20, tzinfo=timezone.utc))


# ============================================================
# Real-entity fingerprints and completeness (isolated exception:
# reads production objects at runtime; fixture carries hashes only)
# ============================================================


def _fingerprint(entity: InvoiceEntity) -> str:
    field_values = {
        "key": entity.key,
        "company_name": entity.company_name,
        "tax_id": entity.tax_id,
        "address": entity.address,
        "bank_account_name": entity.bank_account_name,
        "bank_account_number": entity.bank_account_number,
        "bank_name": entity.bank_name,
        "bank_branch": entity.bank_branch,
        "bank_swift": entity.bank_swift,
    }
    canonical = json.dumps(
        field_values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_real_entity_fingerprints_and_completeness() -> None:
    expected = json.loads((FIXTURES / "invoice_entity_fingerprints.json").read_text("utf-8"))
    assert set(expected) == ENTITY_KEYS
    failures: list[str] = []
    for key, entity in ENTITIES.items():
        missing = entity.missing_fields()
        if missing:
            failures.append(f"{key}: missing {missing}")
            continue
        if _fingerprint(entity) != expected[key]:
            failures.append(f"{key}: fingerprint mismatch")
    assert failures == [], failures


def test_registry_with_a_mismatched_key_is_rejected(monkeypatch):
    default = entities.ENTITIES[entities.DEFAULT_ENTITY_KEY]
    assert not default.missing_fields()
    mismatched = dataclasses.replace(default, key="some_other_key")
    monkeypatch.setattr(entities, "ENTITIES", {entities.DEFAULT_ENTITY_KEY: mismatched})
    with pytest.raises(ValueError, match="registry key does not match"):
        resolve_invoice_entity("kitas", datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc))


_REVIEWED_SAMPLES = {
    # Hand-written from the accounting review sheet, independent of the fixture.
    "visa_b1_voa": "cimb_bayu_bali_nol",
    "visa_c1_tourism": "cimb_bayu_bali_nol",
    "visa_d1_tourism_1yr": "cimb_bayu_bali_nol",
    "visa_d2_business_5yr": "cimb_bayu_bali_nol",
    "visa_d12_business_2yr": "cimb_bayu_bali_nol",
    "ext_c1_tourism": "bni_bayu_santero",
    "other_epo": "cimb_bayu_bali_nol",
    "other_erp": "cimb_bayu_bali_nol",
    "other_cancel_rptka": "cimb_bayu_bali_nol",
    "other_sktt": "cimb_bayu_bali_nol",
    "accessories": "cimb_bayu_bali_nol",
    "visa": "cimb_bayu_bali_nol",
    "kitas": "mandiri_bali_nol_impresariat",
    "kitas_working_onshore": "mandiri_bali_nol_impresariat",
    "kitas_spouse_2yr_extend": "mandiri_bali_nol_impresariat",
    "kitap_investor_merp": "mandiri_bali_nol_impresariat",
    "company_pt_pma": "mandiri_bali_nol_impresariat",
    "company_virtual_office": "mandiri_bali_nol_impresariat",
    "tax_close_pma_company": "mandiri_bali_nol_impresariat",
    "other_custom": "mandiri_bali_nol_impresariat",
    "other_born_report": "mandiri_bali_nol_impresariat",
    "other_lapor_lahir_up": "mandiri_bali_nol_impresariat",
    "pt_pma_setup": "mandiri_bali_nol_impresariat",
    "tax_lkpm_yearly": "permata_bali_nol_konsultan",
    "tax_annual_pkg_d": "permata_bali_nol_konsultan",
    "tax_monthly_bundled_200_plus": "permata_bali_nol_konsultan",
    "ext_b1_voa": "bni_bayu_santero",
    "extension_visa": "bni_bayu_santero",
}


@pytest.mark.parametrize(("code", "key"), sorted(_REVIEWED_SAMPLES.items()))
def test_reviewed_mapping_samples(code: str, key: str) -> None:
    assert PRACTICE_TYPE_ENTITY_KEYS[code] == key


_ISSUER_3_CODES = {"ext_b1_voa", "ext_c1_tourism", "ext_d12", "extension_visa"}


@pytest.mark.parametrize("code", ["ext_c1_tourism"])
def test_moved_codes_resolve_to_issuer_3(code: str) -> None:
    assert entity_key_for_code(code) == "bni_bayu_santero"


def test_issuer_3_covers_exactly_its_four_codes() -> None:
    got = {c for c, k in PRACTICE_TYPE_ENTITY_KEYS.items() if k == "bni_bayu_santero"}
    assert got == _ISSUER_3_CODES


# Team report 2026-11-20: "Other" process invoices must come from issuer 1.
# other_epo and other_erp go back to issuer 1; the issuer 3 extensions stay
# where the 2026-11-18 ruling put them, and other_custom stays on issuer 4.
@pytest.mark.parametrize(
    ("code", "key"),
    [
        ("other_epo", "cimb_bayu_bali_nol"),
        ("other_erp", "cimb_bayu_bali_nol"),
        ("ext_c1_tourism", "bni_bayu_santero"),
        ("ext_b1_voa", "bni_bayu_santero"),
        ("other_custom", "mandiri_bali_nol_impresariat"),
    ],
)
def test_other_epo_erp_back_on_issuer_1(code: str, key: str) -> None:
    assert entity_key_for_code(code) == key
