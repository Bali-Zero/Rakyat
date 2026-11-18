"""Pins the 2026-11-18 price-list change on the canonical catalogue.

Three claims, each failing loudly:

1. the two changed/new rows carry the ruled price and attestation;
2. every OTHER catalogue value is byte-for-byte the pre-change base (the digest
   below covers everything except those rows and ``metadata.last_updated``);
3. the committed site snapshot in apps/mouth equals what the catalogue would
   project to for every row (the full deep-equality lives in the mouth test
   ``pricing-snapshot.test.ts``; this is the backend-side cross-check on the
   rows that moved).

When a LATER change legitimately edits another row, update ``_BASE_DIGEST`` in
the same commit: the digest is a ratchet, not a statement that prices are frozen.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[3]
_CATALOGUE = _BACKEND / "data" / "bali_zero_official_prices_2026.json"
_SNAPSHOT = _BACKEND.parents[1] / "mouth" / "data" / "bali-zero-prices.json"

_ONSHORE = "Working KITAS (Altus/Onshore)"
_DISSOLVING = "Dissolving Company"

#: sha256 over the catalogue minus the rows below and metadata.last_updated.
_BASE_DIGEST = (
    "865c04e15b40ff541bac5859e9be971b2f9dcb4e87cb067b42060d0a22f25b90"  # pragma: allowlist secret
)


def _load() -> dict:
    return json.loads(_CATALOGUE.read_text(encoding="utf-8"))


def _digest(doc: dict) -> str:
    doc = json.loads(json.dumps(doc))
    doc["metadata"].pop("last_updated", None)
    doc["services"]["kitas_permits"].pop(_ONSHORE, None)
    doc["services"]["company_services"].pop(_DISSOLVING, None)
    blob = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_changed_rows_carry_the_ruled_price_and_attestation() -> None:
    doc = _load()
    onshore = doc["services"]["kitas_permits"][_ONSHORE]
    dissolving = doc["services"]["company_services"][_DISSOLVING]
    assert onshore["price"] == "36.500.000 IDR"
    assert dissolving["price"] == "13.000.000 IDR"
    assert onshore["verified_on"] == "2026-11-18"
    assert dissolving["verified_on"] == "2026-11-18"
    assert doc["metadata"]["last_updated"] == "2026-11-18"


def test_every_other_catalogue_value_is_the_base() -> None:
    assert _digest(_load()) == _BASE_DIGEST, (
        "a catalogue value other than the changed rows differs from the base. "
        "If the edit is intended, update _BASE_DIGEST in the same commit."
    )


def test_site_snapshot_matches_the_catalogue_on_the_changed_rows() -> None:
    doc = _load()
    snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["services_by_category"]
    for category, key in (
        ("kitas_permits", _ONSHORE),
        ("company_services", _DISSOLVING),
    ):
        assert snap[category][key]["price"] == doc["services"][category][key]["price"]
