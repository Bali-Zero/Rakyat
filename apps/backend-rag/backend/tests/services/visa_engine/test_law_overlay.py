"""Law projection parity and the display-only ``VISA_ORACLE_LAW_OVERLAY`` overlay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from backend.services.pricing.pricing_service import get_pricing_service
from backend.services.visa_engine import evaluate_path, law_projection
from backend.services.visa_engine.api_models import VisaOracleEvaluateResponse
from backend.services.visa_engine.bundle import StaticTrustStore, verify_rule_pack
from backend.services.visa_engine.compiler import build_compiled_pack
from backend.services.visa_engine.evaluate_path import _build_display, _build_sources_dto
from backend.services.visa_engine.families import importer
from backend.services.visa_engine.pricing_adapter import UnavailablePricingCatalog
from backend.tests.services.visa_engine import test_interview_walk_census as census
from backend.tests.services.visa_engine.test_seq28_pack import PROD_TRUST_STORE_JSON

_PACKS = Path(evaluate_path.__file__).parent / "contracts" / "packs"
_CONTRACT = Path(evaluate_path.__file__).parent / "contracts" / "contract.schema.json"
_CODES = ("C1", "E30A", "E30B", "E30E", "E31B")
_CLAMP_CODES = ("C1", "E28A", "E23", "E30B")


@pytest.fixture(scope="module")
def pack() -> census.PackUnderTest:
    signed = json.loads((_PACKS / "rulepack-prod-031.signed.json").read_text(encoding="utf-8"))
    signed_at = datetime.fromisoformat(signed["protected"]["signed_at"].replace("Z", "+00:00"))
    observed_at = signed_at + timedelta(hours=1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VISA_ENGINE_TRUST_STORE_KEYS_JSON", PROD_TRUST_STORE_JSON)
        verified = verify_rule_pack(
            signed, trust_store=StaticTrustStore.from_env(), observed_at=observed_at
        )
    assert verified.pack.payload.sequence == 31
    return census.PackUnderTest(compiled=build_compiled_pack(verified.pack), as_of=observed_at)


def _display(
    pack: census.PackUnderTest, stay_days: int | None = None, codes: tuple[str, ...] = _CODES
) -> dict[str, Any]:
    """Display for one stand-in decision whose candidates are C1, E30A, E30B, E30E, E31B."""

    products = {p.product_code: p for p in pack.compiled.source_pack.payload.products}
    decision = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                product_code=code, product_version_id=products[code].product_version_id, rank=rank
            )
            for rank, code in enumerate(codes, start=1)
        ],
        evaluated_at=pack.as_of,
        observed_at=pack.as_of,
    )
    display = _build_display(
        decision,  # type: ignore[arg-type]
        pack.compiled,
        request_trace="test",
        pricing_catalog=get_pricing_service(),
        stay_days=stay_days,
    )
    return {c["product_code"]: c for c in display["candidates"]}


# --- projection ----------------------------------------------------------------------


def test_projection_rows_equal_importer_rows_for_every_shipped_family() -> None:
    families = law_projection.shipped_families()
    assert {"C", "E28", "E30", "E31", "E33"} <= set(families)
    rows = [row for family in families for row in importer.build_rows(family)]
    projection = law_projection.law_projection()
    assert sorted(projection) == sorted(row["code"] for row in rows)
    for row in rows:
        assert projection[row["code"]] == law_projection.entry_from_row(row)
        entry = projection[row["code"]]
        assert entry.verification_status == row["verification_status"]
        assert entry.official_name_id == row["official_name_id"]
    assert law_projection.law_projection() is projection  # loaded once per process


def test_every_shipped_family_has_a_migration_that_matches_the_importer() -> None:
    migrations = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
    for family in law_projection.shipped_families():
        files = list(migrations.glob(f"*_visa_types_law_family_{family.lower()}.sql"))
        assert files, family
        # Applied migrations are immutable: a family is checked against its LATEST migration,
        # which is a later bundle once owner rulings changed its cards.
        history = importer.FAMILY_MIGRATIONS[family]
        assert {p.name for p in files} <= set(history), family
        newest = migrations / importer.latest_migration(family)
        assert newest.read_text() == importer.expected_sql(newest.name, family), newest.name


def test_verified_entry_hides_to_verify_and_unknown_codes() -> None:
    assert law_projection.verified_entry("E30B") is not None
    assert law_projection.law_projection()["E30E"].verification_status == "TO_VERIFY"
    assert law_projection.verified_entry("E30E") is None
    # No family ships ZZ99: an unknown code has no entry.
    assert law_projection.verified_entry("ZZ99") is None
    # A1 ships VERIFIED with family A (339), so it is no longer an unknown code.
    assert law_projection.verified_entry("A1") is not None


# --- overlay -------------------------------------------------------------------------


def test_flag_on_uses_the_law_names_for_e30a_and_e30b(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    shown = _display(pack)
    assert shown["E30A"]["name"]["id"] == "E30A Visa Pendidikan Dasar dan Menengah"
    assert shown["E30B"]["name"]["id"] == "E30B Visa Pendidikan Tinggi"
    assert shown["E30B"]["name"]["en"] == "Higher Education Visa (E30B)"
    assert shown["E30B"]["stay_policy"]["stay"] == {
        "kind": "FIXED_DAYS",
        "minimum_days": 365,
        "maximum_days": 1460,
    }
    marker = shown["E30B"]["law_overlay"]
    assert marker["applied"] is True and marker["verification_status"] == "VERIFIED"
    assert marker["card_sha256"] == law_projection.verified_entry("E30B").card_sha256


def test_flag_on_c1_stays_60_days_extendable_to_180(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    c1 = _display(pack)["C1"]
    assert c1["name"]["id"] == "C1 Visa Kunjungan Wisata"
    assert c1["stay_policy"]["stay"] == {
        "kind": "FIXED_DAYS",
        "minimum_days": 60,
        "maximum_days": 60,
    }
    assert c1["stay_policy"]["extension"]["allowed"] is True
    assert c1["law_overlay"]["extendable"] is True
    assert c1["law_overlay"]["max_total_days"] == 180
    assert c1["law_overlay"]["max_total_months"] is None


def test_flag_on_leaves_prices_and_to_verify_codes_alone(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "off")
    before = _display(pack, stay_days=400)
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    after = _display(pack, stay_days=400)
    assert after["E31B"]["pricing"] == before["E31B"]["pricing"]
    assert after["E31B"]["pricing"]["status"] == "AVAILABLE"
    assert after["E31B"]["duration_options"] == before["E31B"]["duration_options"]
    assert after["E30E"] == before["E30E"]
    assert "law_overlay" not in after["E30E"]
    for code in _CODES:
        changed = {k for k in after[code] if after[code][k] != before[code].get(k)}
        assert changed <= {"name", "stay_policy", "law_overlay"}, code


def test_non_extendable_law_card_shows_a_verified_no_extension(monkeypatch) -> None:
    entry = law_projection.verified_entry("C18")
    assert entry is not None and entry.extendable is False
    product = SimpleNamespace(names=SimpleNamespace(en="Skill Test Visit Visa (C18)"))
    shown: dict[str, Any] = {
        "product_code": "C18",
        "name": {"id": "x", "en": "y"},
        "stay_policy": {"stay": {}, "extension": {}},
    }
    assert evaluate_path._apply_law_overlay(shown, product)  # type: ignore[arg-type]
    assert shown["stay_policy"]["extension"] == {
        "status": "VERIFIED",
        "reason_code": None,
        "allowed": False,
        "maximum_extensions": 0,
        "days_per_extension": None,
    }
    assert shown["stay_policy"]["stay"]["maximum_days"] == 90


def test_a_broken_projection_falls_back_to_the_pack_display(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "off")
    before = _display(pack)

    def broken(_code: str) -> None:
        raise OSError("cards missing")

    monkeypatch.setattr(evaluate_path, "verified_entry", broken)
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    assert _display(pack) == before


@pytest.mark.parametrize("value", [None, "off", "OFF", "", "yes", "true", "1"])
def test_flag_off_is_byte_identical_to_the_pack_display(pack, monkeypatch, value) -> None:
    """Golden: anything but ``on`` never reaches the overlay and changes no byte."""

    monkeypatch.delenv("VISA_ORACLE_LAW_OVERLAY", raising=False)
    golden = json.dumps(_display(pack), sort_keys=True)

    def must_not_run(*_args: Any) -> bool:
        raise AssertionError("overlay ran with the flag off")

    monkeypatch.setattr(evaluate_path, "_apply_law_overlay", must_not_run)
    if value is not None:
        monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", value)
    assert json.dumps(_display(pack), sort_keys=True) == golden
    assert "law_overlay" not in golden


def test_flag_on_responses_validate_and_keep_the_decision(pack, monkeypatch) -> None:
    """Every census walk on the live pack: overlaid responses satisfy the DTO and the
    committed contract, and the decision block is the one the flag-off response carries."""

    contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        {"$defs": contract["$defs"], "$ref": "#/$defs/VisaOracleEvaluateResponse"}
    )
    overlaid = 0
    for label, spec in census._load_walks().items():
        decision, compiled, _request = census._decide(spec["overrides"], label, pack)
        if not decision.candidates:
            continue
        payloads = {}
        for flag in ("off", "on"):
            monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", flag)
            payloads[flag] = VisaOracleEvaluateResponse.model_validate(
                {
                    "mode": "ENGINE",
                    "decision": decision.model_dump(mode="json"),
                    "sources": _build_sources_dto(decision, compiled, request_trace="t"),
                    "display": _build_display(
                        decision,
                        compiled,
                        request_trace="t",
                        pricing_catalog=UnavailablePricingCatalog(),
                    ),
                }
            ).model_dump(mode="json")
            assert list(validator.iter_errors(payloads[flag])) == [], label
        assert payloads["on"]["decision"] == payloads["off"]["decision"]
        assert payloads["on"]["sources"] == payloads["off"]["sources"]
        overlaid += sum("law_overlay" in c for c in payloads["on"]["display"]["candidates"])
        assert not any("law_overlay" in c for c in payloads["off"]["display"]["candidates"])
    assert overlaid


# --- sold-options clamp ----------------------------------------------------------------


def _stay(card: dict[str, Any]) -> tuple[int, int]:
    stay = card["stay_policy"]["stay"]
    return stay["minimum_days"], stay["maximum_days"]


def test_sold_durations_read_the_price_key_text_then_the_catalog_row() -> None:
    from backend.services.visa_engine import sold_durations as sd

    assert sd.parse_duration_days("Investor KITAS 2 Years (Offshore)") == 730
    assert sd.parse_duration_days("Dependent 1 Year (Offshore)") == 365
    assert sd.parse_duration_days("Freelance 6 months") == 180
    assert sd.parse_duration_days("C1 Tourism") is None
    assert sd.parse_duration_days("1 year or 2 years") is None  # ambiguous: nothing sold
    assert sd.parse_duration_days(None) is None
    # Ranges are ambiguous, never the upper bound.
    for text in ("1-2 years", "1 to 2 Years", "1/2 years", "6 or 12 months"):
        assert sd.parse_duration_days(text) is None, text
    # Whole years of months are 365 days each; other months are 30 days.
    assert sd.parse_duration_days("12 months") == 365
    assert sd.parse_duration_days("24 months") == 730
    assert sd.parse_duration_days("6 months") == 180


def test_flag_on_e28a_shows_only_the_sold_730_days_and_marks_the_clamp(pack, monkeypatch) -> None:
    law = law_projection.verified_entry("E28A")
    assert law is not None and law.stay_option_days() == (365, 730)
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "off")
    before = _display(pack, codes=_CLAMP_CODES)
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    shown = _display(pack, codes=_CLAMP_CODES)
    assert _stay(shown["E28A"]) == (730, 730)
    assert shown["E28A"]["law_overlay"]["stay_clamp"] == {
        "law_options_days": [365, 730],
        "sold_days": [730],
        "shown_days": [730],
        "clamped_days": [365],
        "fallback": False,
    }
    assert shown["E28A"]["pricing"] == before["E28A"]["pricing"]


def test_flag_on_e23_shows_only_the_sold_365_days_when_the_card_ships(pack, monkeypatch) -> None:
    law = law_projection.law_projection().get("E23")
    if law is None or not law.verified:
        pytest.skip("E23 Law card not on this base yet")
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    assert _stay(_display(pack, codes=_CLAMP_CODES)["E23"]) == (365, 365)


def test_nothing_sold_in_common_falls_back_to_the_pack_stay(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "off")
    before = _display(pack, codes=_CLAMP_CODES)
    monkeypatch.setattr(evaluate_path, "sold_duration_days", lambda _product: frozenset({999}))
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    shown = _display(pack, codes=_CLAMP_CODES)
    assert shown["E28A"]["stay_policy"]["stay"] == before["E28A"]["stay_policy"]["stay"]
    clamp = shown["E28A"]["law_overlay"]["stay_clamp"]
    assert clamp["fallback"] is True and clamp["shown_days"] == []
    monkeypatch.setattr(evaluate_path, "sold_duration_days", lambda _product: frozenset())
    shown = _display(pack, codes=_CLAMP_CODES)
    assert shown["E28A"]["stay_policy"]["stay"] == before["E28A"]["stay_policy"]["stay"]


def test_flag_on_c1_has_no_clamp_and_stays_unchanged(pack, monkeypatch) -> None:
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    c1 = _display(pack, codes=_CLAMP_CODES)["C1"]
    assert _stay(c1) == (60, 60)
    assert "stay_clamp" not in c1["law_overlay"]


def test_clamp_marker_validates_against_the_dto_and_the_flag_off_golden(pack, monkeypatch) -> None:
    from backend.services.visa_engine.api_models import CandidateDisplayDTO

    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    for card in _display(pack, codes=_CLAMP_CODES).values():
        if "law_overlay" not in card:
            continue
        dumped = CandidateDisplayDTO.model_validate(card).model_dump(mode="json")
        assert ("stay_clamp" in dumped["law_overlay"]) == ("stay_clamp" in card["law_overlay"])


def test_single_option_cards_keep_the_plain_overlay_and_get_no_clamp(pack, monkeypatch) -> None:
    law = law_projection.verified_entry("BRIDGING")
    assert law is not None and law.stay_option_days() == (60,)
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    # A sold set that excludes 60 must not matter: one option is never clamped.
    monkeypatch.setattr(evaluate_path, "sold_duration_days", lambda _product: frozenset({999}))
    shown = _display(pack, codes=("BRIDGING",))["BRIDGING"]
    assert _stay(shown) == (60, 60)
    assert "stay_clamp" not in shown["law_overlay"]


def test_a_malformed_catalog_row_falls_back_for_that_product_only(pack, monkeypatch) -> None:
    from backend.services.visa_engine import sold_durations as sd

    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "off")
    before = _display(pack, codes=_CLAMP_CODES)
    monkeypatch.setattr(sd, "_services", lambda: {"kitas_permits": ["not", "a", "dict"]})
    monkeypatch.setenv("VISA_ORACLE_LAW_OVERLAY", "on")
    shown = _display(pack, codes=_CLAMP_CODES)
    assert shown["E28A"]["stay_policy"]["stay"] == before["E28A"]["stay_policy"]["stay"]
    assert shown["E28A"]["law_overlay"]["stay_clamp"]["fallback"] is True
    assert _stay(shown["C1"]) == (60, 60)  # the rest of the response is still overlaid
    monkeypatch.setattr(sd, "_services", lambda: {"kitas_permits": {"x": "not a row"}})
    assert sd.sold_duration_days(next(iter(_products(pack).values()))) == frozenset()


def _products(pack: census.PackUnderTest) -> dict[str, Any]:
    return {p.product_code: p for p in pack.compiled.source_pack.payload.products}
