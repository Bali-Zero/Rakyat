"""Kita final delivery requirements. The CSV is also the reviewable service catalogue."""

import csv
from functools import cache
from pathlib import Path
from typing import TypedDict


class DeliverySlot(TypedDict):
    key: str
    label: str
    condition: str | None


class DeliveryPolicy(TypedDict):
    slots: list[DeliverySlot]
    bonus_slot: str | None
    bonus_trigger: str
    followup_slot: str | None
    can_postpone: bool


# Legacy codes still occur on open practices created before the catalogue was consolidated.
_LEGACY_CODES = {"visa_voa": "visa_b1_voa", "voa_extension": "ext_b1_voa"}


def _slot(index: int, value: str) -> DeliverySlot:
    label, _, qualifier = value.partition(" · ")
    # A timing instruction (after entry) or a redaction requirement is mandatory.
    # Only a scope-dependent item may be waived, with a reason, by the operator.
    conditional = qualifier.startswith(("if ", "only if ", "per filing due"))
    if qualifier and not conditional:
        label = value
    # Slot numbers are persistent identities within a service. Append new slots
    # at the end. Wording and translations can change without orphaning uploads.
    key = "electronic_visa" if label.startswith("Electronic Visa") else f"document_{index}"
    return {"key": key, "label": label, "condition": qualifier if conditional else None}


@cache
def _catalog() -> dict[str, DeliveryPolicy]:
    result = {}
    with Path(__file__).with_suffix(".csv").open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            slots = [
                _slot(i, row[f"upload_box_{i}"]) for i in range(1, 7) if row[f"upload_box_{i}"]
            ]
            trigger = row["bonus_trigger"]
            if trigger == "electronic_visa_issued":
                bonus_slot = "electronic_visa"
            elif trigger == "kitas_imk_uploaded":
                bonus_slot = next(s["key"] for s in slots if s["label"].startswith("Renewed IMK"))
            elif trigger == "service_delivered":
                bonus_slot = None
            else:
                raise ValueError(f"Unknown delivery bonus trigger for {row['service_code']}")
            if bonus_slot and not any(slot["key"] == bonus_slot for slot in slots):
                raise ValueError(f"Missing bonus evidence for {row['service_code']}")
            followup_slot = (
                slots[int(row["followup_box"]) - 1]["key"] if row["followup_box"] else None
            )
            result[row["service_code"]] = DeliveryPolicy(
                slots=slots,
                bonus_slot=bonus_slot,
                bonus_trigger=trigger,
                followup_slot=followup_slot,
                can_postpone=bool(followup_slot and not row["service_code"].endswith("_onshore")),
            )
    return result


def delivery_policy(code: str) -> DeliveryPolicy | None:
    """Unknown and open-inquiry codes require a defined service before delivery."""
    return _catalog().get(_LEGACY_CODES.get(code, code))
