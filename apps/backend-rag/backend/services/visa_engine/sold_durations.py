"""Durations Bali Zero actually sells for a product, read from its price keys.

The Law may allow stays Bali Zero does not sell (E28A: 1-2 years, only the 2-year key is
priced). The Law overlay clamps its display to these durations. A duration comes from the
price key's own text ("... 2 Years ...", "... 6 months") or, failing that, from the catalog
row's ``duration`` / ``validity`` text. A key with no readable duration sells nothing here.
Display-only; read once per process from the committed official price list.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from backend.services.visa_engine.models import VisaProductVersion

_PRICES = Path(__file__).resolve().parents[2] / "data" / "bali_zero_official_prices_2026.json"
_RANGE_RE = re.compile(r"\d\s*(?:-|\u2013|\u2014|/|to|or|and)\s*\d", re.IGNORECASE)
_DURATION_RE = re.compile(r"(\d+)\s*(years?|yrs?|months?|days?)\b", re.IGNORECASE)


@cache
def _services() -> dict[str, Any]:
    return json.loads(_PRICES.read_text(encoding="utf-8"))["services"]


def parse_duration_days(text: object) -> int | None:
    """Days named by ``text`` ("2 Years" -> 730, "6 months" -> 180); ``None`` when absent or
    ambiguous (more than one distinct duration)."""

    if not isinstance(text, str) or _RANGE_RE.search(text):
        return None
    found: set[int] = set()
    for amount, unit in _DURATION_RE.findall(text):
        n = int(amount)
        unit = unit.lower()
        if unit.startswith("y"):
            found.add(n * 365)
        elif unit.startswith("m"):
            # Whole years are 365 days each (12 -> 365, 24 -> 730); other months are 30 days.
            found.add(n // 12 * 365 if n % 12 == 0 else n * 30)
        else:
            found.add(n)
    return found.pop() if len(found) == 1 else None


def sold_duration_days(product: VisaProductVersion) -> frozenset[int]:
    """Durations (days) derivable from the product's price keys; empty when none is."""

    keys = [product.pricing_key] + [o.pricing_key for o in product.duration_options or ()]
    days: set[int] = set()
    for key in keys:
        if key is None:
            continue
        category = _services().get(key.category)
        row = category.get(key.item_key) if isinstance(category, dict) else None
        if not isinstance(row, dict):
            continue
        for text in (key.item_key, row.get("duration"), row.get("validity")):
            parsed = parse_duration_days(text)
            if parsed is not None:
                days.add(parsed)
                break
    return frozenset(days)
