"""PPh withholding chosen by the tax team per process (K127).

A practice may carry ``metadata.pph = {"type": "PPH23", "rate_percent": 2}``. When it
does, the invoice shows the tax base (DPP = quoted price − discount), the withheld PPh
and the amount the client must transfer (DPP − PPh). Without it the invoice is exactly
what the system issued before.

One module so the CRM router (validation), the invoice service (amounts persisted to
``invoices.amount_idr``) and the PDF generator (label) cannot drift apart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

#: Allowed withholding types and how each is printed on the invoice.
PPH_TYPE_LABELS: dict[str, str] = {
    "PPH23": "PPh 23",
    "PPH21": "PPh 21",
    "PPH4_2": "PPh 4(2)",
}
PPH_MAX_RATE_PERCENT = Decimal("10")


@dataclass(frozen=True)
class PphWithholding:
    """A validated PPh selection: type from ``PPH_TYPE_LABELS``, 0 < rate ≤ 10, ≤ 2 decimals."""

    type: str
    rate_percent: Decimal

    @property
    def label(self) -> str:
        """Totals-row label, e.g. ``Tax PPh 23 2%``."""
        rate = format(self.rate_percent.normalize(), "f")
        return f"Tax {PPH_TYPE_LABELS[self.type]} {rate}%"

    def amount_on(self, dpp: Decimal | float | int) -> int:
        """PPh on ``dpp`` rounded to the rupiah, half up (1,234,567 × 2% → 24,691)."""
        base = Decimal(str(dpp))
        return int((base * self.rate_percent / Decimal(100)).quantize(Decimal(1), ROUND_HALF_UP))

    def as_metadata(self) -> dict[str, Any]:
        """Shape stored in ``practices.metadata.pph`` (a JSON number for the rate)."""
        return {"type": self.type, "rate_percent": float(self.rate_percent)}


def validate_pph(type_: str, rate_percent: Decimal) -> PphWithholding:
    """Raise ``ValueError`` unless the selection is one the tax team may issue."""
    if type_ not in PPH_TYPE_LABELS:
        raise ValueError(f"pph.type must be one of {sorted(PPH_TYPE_LABELS)}")
    if not rate_percent.is_finite() or rate_percent <= 0 or rate_percent > PPH_MAX_RATE_PERCENT:
        raise ValueError("pph.rate_percent must be greater than 0 and at most 10")
    if rate_percent != rate_percent.quantize(Decimal("0.01")):
        raise ValueError("pph.rate_percent allows at most 2 decimals")
    return PphWithholding(type=type_, rate_percent=rate_percent)


def pph_from_metadata(metadata: Any, *, practice_id: int | None = None) -> PphWithholding | None:
    """Read ``metadata.pph`` from a practice row; ``None`` when absent.

    The CRM router validates every write, so an unreadable value can only come
    from a hand edit: it is logged and ignored (the invoice is issued without
    PPh, as before this feature) rather than blocking billing.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return None
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("pph")
    if raw is None:
        return None
    try:
        return validate_pph(str(raw["type"]), Decimal(str(raw["rate_percent"])))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        logger.warning("Ignoring unreadable metadata.pph on practice %s", practice_id)
        return None
