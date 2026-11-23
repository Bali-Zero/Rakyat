"""Pins the 2026-11-18 price-list change on the canonical catalogue.

Three claims, each failing loudly:

1. the two changed/new rows carry the ruled price and attestation;
2. every OTHER catalogue value is the pre-change base: the digest below covers
   everything except ``metadata.last_updated`` and the onshore row's price and
   date, and the new row is compared whole with the ruled row;
3. the committed site snapshot in apps/mouth says the same on the rows that
   moved, in both of its shapes (the full deep-equality lives in the mouth test
   ``pricing-snapshot.test.ts``), and ``llms.txt`` states the onshore price the
   catalogue carries.

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

_LLMS = _BACKEND.parents[1] / "mouth" / "public" / "llms.txt"

#: sha256 over the catalogue minus what this change moved: metadata.last_updated,
#: the onshore row's price and verified_on (its other fields stay inside the
#: digest), and the new row, which is pinned whole by _DISSOLVING_ROW instead.
_BASE_DIGEST = (
    "2a8d0f8c782aea9588c84e2e0c1c85e0b39ae54db9fa8a8d1f5ae89b7c7a66fa"  # pragma: allowlist secret
)

_DISSOLVING_ROW = {
    "name": "Dissolving Company",
    "price": "13.000.000 IDR",
    "tier_range": None,
    "duration": "",
    "validity": "",
    "notes": "",
    "verified_on": "2026-11-18",
    "description_en": (
        "Dissolution and liquidation of an existing Indonesian limited company, "
        "including the notarial dissolution deed and deregistration with the tax "
        "and licensing authorities."
    ),
    "icon_id": "company-close",
}


def _load() -> dict:
    return json.loads(_CATALOGUE.read_text(encoding="utf-8"))


def _digest(doc: dict) -> str:
    doc = json.loads(json.dumps(doc))
    doc["metadata"].pop("last_updated", None)
    onshore = doc["services"]["kitas_permits"][_ONSHORE]
    onshore.pop("price", None)
    onshore.pop("verified_on", None)
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


def test_the_new_row_is_exactly_the_ruled_row() -> None:
    assert _load()["services"]["company_services"][_DISSOLVING] == _DISSOLVING_ROW


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


def test_site_snapshot_card_entry_of_the_new_row_matches_the_catalogue() -> None:
    row = _load()["services"]["company_services"][_DISSOLVING]
    cards = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))["company_services"]
    assert cards[row["icon_id"]] == {
        "name": row["name"],
        "price": row["price"],
        "icon_id": row["icon_id"],
    }


def test_llms_txt_states_the_catalogue_onshore_price() -> None:
    amount = _load()["services"]["kitas_permits"][_ONSHORE]["price"].removesuffix(" IDR")
    lines = [
        line
        for line in _LLMS.read_text(encoding="utf-8").splitlines()
        if "Working KITAS Onshore" in line
    ]
    assert lines == [f"- E23 - Working KITAS Onshore: IDR {amount}"]
