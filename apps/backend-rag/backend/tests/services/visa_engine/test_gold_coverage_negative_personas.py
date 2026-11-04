"""Negative gold personas for the E30 (study) family and E31C (family).

The positive corpus under ``gold_coverage/personas/`` is POSITIVE-ONLY by
design: ``test_gold_coverage_floor.py`` requires every file there to replay
SUPPORTED for its own product, and ``test_seq19_pack.py`` /
``test_seq20_pack.py`` replay every file there as SUPPORTED.  A persona whose
WHOLE POINT is that the engine must reject or redirect it (a minor without a
confirmed sponsor, an admission never confirmed, a sponsor of the wrong kind)
can therefore never live in that directory: adding it there would either break
the floor or force a count pin to move, and the seq-19/20 packs would replay
it against expectations they do not carry.

This module is the armed twin for a NEW sibling corpus,
``gold_coverage/negative_personas/``: each file there is derived from the
SAME-PRODUCT positive persona in ``personas/`` by changing exactly ONE
intended fact (the twin test below pins which fact, so the failing sibling
differs in one fact and not by accident of drift).  It pins, against the
highest signed PRODUCTION pack:

* E30A__minor -> NO_SUPPORTED_PATH with MINOR_SPONSOR_NOT_CONFIRMED +
  LEVEL_BAND_DIKTI (guardian consent alone does not sponsor a minor);
* E30B__admission_false -> NO_SUPPORTED_PATH with
  STUDY_ADMISSION_OR_SPONSOR_NOT_CONFIRMED + LEVEL_BAND_DASMEN;
* E30F__individual_sponsor -> SUPPORTED_CANDIDATES but with E30F itself
  absent from the candidates (an INDIVIDUAL sponsor does not qualify the
  exchange visa; the engine must redirect, not dead-end);
* E31C__marriage_unregistered -> SUPPORTED_CANDIDATES with only E31F: a KNOWN
  "parents' marriage not registered" drops E31C (child of a registered
  WNA-WNI marriage) and the engine redirects to E31F, measured on seq-29.

The LEVEL_BAND_* codes ride along with the named cause and are pinned only because they
were measured on seq-28; they follow the study level, not the negative fact under test.

A missing artifact FAILS here, never skips: the directory must hold exactly
the four pinned file names, and every file name's ``__``-prefix must equal
its ``product_code`` (same rule as the floor test).  Each positive twin is
also replayed to SUPPORTED with its own product — a negative whose twin is
already broken would be vacuous, and this guards against that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.visa_engine.gold_coverage_eval import _evaluate, _verified_compiled_pack
from backend.scripts.visa_engine.gold_replay_driver import (
    PACKS_DIR,
    _parse_utc,
    build_persona_request,
    select_highest_repository_pack,
)
from backend.services.visa_engine import evaluator
from backend.services.visa_engine.enums import DecisionState
from backend.services.visa_engine.fact_registry import DEFAULT_FACT_REGISTRY
from backend.tests.services.visa_engine.test_evaluator_gold import Persona
from backend.tests.services.visa_engine.test_interview_walk_census import (
    _route_not_assessed_codes,
)

NEGATIVE_DIR = Path(__file__).resolve().parent / "gold_coverage" / "negative_personas"
POSITIVE_DIR = Path(__file__).resolve().parent / "gold_coverage" / "personas"

EXPECTED_FILE_NAMES = frozenset(
    {
        "E30A__minor.json",
        "E30B__admission_false.json",
        "E30F__individual_sponsor.json",
        "E31C__marriage_unregistered.json",
    }
)

#: The ONE intended fact change per negative persona, relative to its
#: positive twin ``personas/<product_code>.json``.  Pinned as data (not
#: re-derived from the files) so a twin that drifts by an extra fact goes red
#: here instead of silently moving the goalposts.
_INTENDED_FACT_DIFFS = {
    "E30A__minor": {
        "person.birth_date": {"status": "KNOWN", "value": "2014-05-20"},
        "person.guardian_consent": {"status": "KNOWN", "value": True},
    },
    "E30B__admission_false": {
        "study.admission_confirmed": {"status": "KNOWN", "value": False},
    },
    "E30F__individual_sponsor": {
        "sponsor.type": {"status": "KNOWN", "value": "INDIVIDUAL"},
    },
    "E31C__marriage_unregistered": {
        "family.marriage_registered": {"status": "KNOWN", "value": False},
    },
}

# PINNED to the highest signed pack's own `signed_at`, never the wall clock:
# the selected pack's source_records carry a freshness_policy
# (MAX_AGE_SINCE_VERIFIED_AT) with as little as a 604800s (7-day) window, and
# `--as-of` also drives `verify_rule_pack`'s `observed_at`, which rejects a
# signature dated AFTER the observation instant.  Same rationale as
# test_gold_coverage_floor.py; see its module docstring for the 2026-08-30
# merge-queue incident that motivates pinning the instant at all.
_, _HIGHEST_SIGNED_PACK = select_highest_repository_pack(PACKS_DIR)
_AS_OF = _parse_utc(_HIGHEST_SIGNED_PACK["protected"]["signed_at"])

_PERSONA_PATHS = sorted(NEGATIVE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_negative_corpus_is_exactly_the_expected_files() -> None:
    """A missing artifact FAILS here; nothing in this module may skip."""
    files = {p.name for p in NEGATIVE_DIR.glob("*.json")}
    assert files == set(EXPECTED_FILE_NAMES), (
        f"negative corpus drifted: expected exactly {sorted(EXPECTED_FILE_NAMES)}, "
        f"found {sorted(files)}"
    )
    assert sorted(EXPECTED_FILE_NAMES), "EXPECTED_FILE_NAMES must stay non-empty"


@pytest.mark.parametrize("persona_path", _PERSONA_PATHS, ids=lambda p: p.stem)
def test_negative_persona_decides_as_pinned(persona_path: Path) -> None:
    spec = _load(persona_path)
    out = _evaluate(spec["overrides"], spec["label"], as_of=_AS_OF)
    actual = out["actual"]

    assert actual["state"] == spec["expected_state"], (
        f"{persona_path.name}: state {actual['state']} != {spec['expected_state']}"
    )
    assert sorted(actual["candidates"] or []) == sorted(spec["expected_candidates"]), (
        f"{persona_path.name}: candidates {sorted(actual['candidates'] or [])} "
        f"!= {sorted(spec['expected_candidates'])}"
    )
    for code in spec.get("expected_absent") or []:
        assert code not in (actual["candidates"] or []), (
            f"{persona_path.name}: {code} must be ABSENT but is in candidates"
        )
    assert sorted(actual["no_path_reason_codes"] or []) == sorted(
        spec["expected_no_path_reason_codes"]
    ), (
        f"{persona_path.name}: no_path_reason_codes "
        f"{sorted(actual['no_path_reason_codes'] or [])} "
        f"!= {sorted(spec['expected_no_path_reason_codes'])}"
    )


@pytest.mark.parametrize("persona_path", _PERSONA_PATHS, ids=lambda p: p.stem)
def test_negative_persona_file_name_matches_its_product_code(persona_path: Path) -> None:
    """``<CODE>__CASE.json`` must declare ``product_code == CODE``.

    Same rule as the floor test: the prefix prevents a renamed fixture from
    silently claiming another product's expectations.
    """
    spec = _load(persona_path)
    product_code = persona_path.stem.split("__", 1)[0]
    assert spec.get("product_code") == product_code, persona_path.name


@pytest.mark.parametrize("persona_path", _PERSONA_PATHS, ids=lambda p: p.stem)
def test_negative_persona_differs_from_positive_twin_in_exactly_the_intended_facts(
    persona_path: Path,
) -> None:
    """The failing sibling must differ from its positive twin in ONE intended
    fact (two for E30A__minor: birth date plus the added guardian consent), so
    the pin isolates the fact under test and not drift in the shared facts."""
    spec = _load(persona_path)
    product_code = spec["product_code"]
    twin = _load(POSITIVE_DIR / f"{product_code}.json")

    twin_overrides = twin["overrides"]
    negative_overrides = spec["overrides"]
    assert set(negative_overrides) == set(twin_overrides) | set(
        _INTENDED_FACT_DIFFS[persona_path.stem]
    ), (
        f"{persona_path.name}: override keys must be the twin's keys plus the "
        "intended added facts — an unexpected key was added or dropped"
    )
    for path, fact in _INTENDED_FACT_DIFFS[persona_path.stem].items():
        assert negative_overrides.get(path) == fact, (
            f"{persona_path.name}: intended fact {path} = "
            f"{negative_overrides.get(path)} != {fact}"
        )
    changed = {
        path
        for path in set(twin_overrides) | set(negative_overrides)
        if negative_overrides.get(path) != twin_overrides.get(path)
    }
    assert changed == set(_INTENDED_FACT_DIFFS[persona_path.stem]), (
        f"{persona_path.name}: facts changed relative to twin {product_code}.json: "
        f"{sorted(changed)}; only the intended facts may differ"
    )


@pytest.mark.parametrize("persona_path", _PERSONA_PATHS, ids=lambda p: p.stem)
def test_positive_twin_is_still_supported_for_its_product(persona_path: Path) -> None:
    """A negative persona is only meaningful against a twin that still plays:
    if the positive twin itself stopped replaying SUPPORTED, the negative's
    miss would be vacuous (it would fail for the wrong reason)."""
    spec = _load(persona_path)
    product_code = spec["product_code"]
    twin = _load(POSITIVE_DIR / f"{product_code}.json")
    out = _evaluate(twin["overrides"], twin["label"], as_of=_AS_OF)
    actual = out["actual"]
    # Products the signed pack PARKS (`*_ROUTE_NOT_ASSESSED`, seq-32: E30E/E30F) can no longer be
    # SUPPORTED: the twin must end EXCLUDED with exactly that reason (asserted, not skipped).
    parked = _route_not_assessed_codes()
    if product_code in parked:
        assert product_code not in (actual["candidates"] or []), (
            f"{persona_path.name}: parked {product_code} must not be a candidate"
        )
        expected_reason = parked[product_code]
        assert expected_reason == f"{product_code}_ROUTE_NOT_ASSESSED"
        _, compiled = _verified_compiled_pack(_AS_OF)
        persona = Persona(
            id=0,
            label=twin["label"],
            overrides=twin["overrides"],
            expected_state=DecisionState.NEEDS_INPUT,
        )
        snapshot = DEFAULT_FACT_REGISTRY.derive(
            build_persona_request(persona).applicant_facts(), effective_at=_AS_OF
        )
        product = next(p for p in compiled.products if p.product_code == product_code)
        proof = evaluator.evaluate_product(
            product=product,
            rules=compiled.rules_for(product, effective_at=_AS_OF),
            facts=snapshot,
            purposes=frozenset({"STUDY"}),
        )
        assert proof.status is evaluator.ProductProofStatus.EXCLUDED, (product_code, proof.status)
        assert [r.code for r in proof.reasons] == [expected_reason], product_code
        return
    assert actual["state"] == twin["expected_state"] == "SUPPORTED_CANDIDATES", (
        f"{persona_path.name}: positive twin {product_code}.json replays "
        f"{actual['state']}, the negative pin is vacuous"
    )
    assert product_code in (actual["candidates"] or []), (
        f"{persona_path.name}: positive twin {product_code}.json lost its product "
        f"from candidates {sorted(actual['candidates'] or [])}"
    )


def test_a_non_parked_product_still_has_to_be_supported() -> None:
    """Guilt check: the parked carve-out is derived from the pack and does not swallow
    every product — E30B is not parked, so its twin stays under the SUPPORTED assertion."""
    assert "E30B" not in _route_not_assessed_codes()
