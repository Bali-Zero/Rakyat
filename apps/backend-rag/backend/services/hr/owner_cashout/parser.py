"""Parser for WEEKLY CASHOUT sheet rows.

BZ schema (9 cols): NAME | PROCESS | PNBP | URGENT | RPTKA/IMTA | TOTAL_INCOME | MARGIN_BS | MARGIN_BZ | NOTE
BS schema (7 cols): NAME | PROCESS | PNBP | URGENT | RPTKA/IMTA | MARGIN_BS | FINAL_PRICE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CashoutRow:
    entity: str  # 'BZ' | 'BS'
    row_index: int
    client_name: str
    process: str | None
    pnbp_idr: int
    urgent_idr: int
    rptka_imta_idr: int
    total_income_idr: int  # only BZ, 0 for BS
    margin_bs_idr: int
    margin_bz_idr: int  # only BZ, 0 for BS
    final_price_idr: int  # only BS, 0 for BZ
    note: str | None


def parse_idr(value: Any) -> int:
    """Parse IDR string like 'Rp1,000,000' to int.

    Returns 0 for empty/None/invalid.
    """
    if value is None:
        return 0
    s = str(value).strip()
    if not s or s in ("-", "—"):
        return 0
    cleaned = s.replace("Rp", "").replace(",", "").replace(".", "").strip()
    if not cleaned:
        return 0
    try:
        return int(cleaned)
    except ValueError:
        logger.warning("[CASHOUT] Failed to parse IDR: %r", value)
        return 0


# A footer label is made only of these words, and carries at least one of the
# anchor words. A client name has a word outside the vocabulary, so it stays a client.
_SUMMARY_VOCABULARY = frozenset({"total", "subtotal", "grand", "final", "fee", "bz", "bs"})
_SUMMARY_ANCHORS = frozenset({"total", "subtotal", "fee"})


def _is_summary_label(name: str) -> bool:
    """True when the NAME cell is a footer label made only of total/fee words, not a client.

    The label is casefolded, trimmed, its inner whitespace collapsed and a trailing
    colon dropped, then split into words. It is a summary row when every word is in
    the vocabulary and at least one word is an anchor (total, subtotal, fee). A client
    such as "Totalindo Contoh" or "Final Contoh" has a word outside the vocabulary.
    """
    label = " ".join(name.casefold().split()).rstrip(":").rstrip()
    tokens = label.split()
    return (
        bool(tokens)
        and all(t in _SUMMARY_VOCABULARY for t in tokens)
        and any(t in _SUMMARY_ANCHORS for t in tokens)
    )


def parse_bz_tab(rows: list[list[str]]) -> list[CashoutRow]:
    """Parse a BZ weekly tab. Rows 1-2 are title+header, data starts at row 3.

    Empty rows are visual separators and must be skipped; so are summary rows
    (TOTAL / FEE footers), which are not clients.
    """
    out: list[CashoutRow] = []
    # rows[0] = title, rows[1] = header, data from rows[2]
    for i, row in enumerate(rows[2:], start=3):
        # Pad row to 9 columns to avoid IndexError
        padded = (list(row) + [""] * 9)[:9]
        name = str(padded[0]).strip() if padded[0] else ""
        if not name:
            continue  # separator row
        if _is_summary_label(name):
            continue  # TOTAL / FEE footer, not a client
        out.append(
            CashoutRow(
                entity="BZ",
                row_index=i,
                client_name=name,
                process=(str(padded[1]).strip() or None),
                pnbp_idr=parse_idr(padded[2]),
                urgent_idr=parse_idr(padded[3]),
                rptka_imta_idr=parse_idr(padded[4]),
                total_income_idr=parse_idr(padded[5]),
                margin_bs_idr=parse_idr(padded[6]),
                margin_bz_idr=parse_idr(padded[7]),
                final_price_idr=0,
                note=(str(padded[8]).strip() or None),
            )
        )
    return out


def parse_bs_tab(rows: list[list[str]]) -> list[CashoutRow]:
    """Parse a BS weekly tab. Schema has 7 columns (no TOTAL INCOME / MARGIN BZ)."""
    out: list[CashoutRow] = []
    for i, row in enumerate(rows[2:], start=3):
        padded = (list(row) + [""] * 7)[:7]
        name = str(padded[0]).strip() if padded[0] else ""
        if not name:
            continue
        if _is_summary_label(name):
            continue  # TOTAL / FEE footer, not a client
        out.append(
            CashoutRow(
                entity="BS",
                row_index=i,
                client_name=name,
                process=(str(padded[1]).strip() or None),
                pnbp_idr=parse_idr(padded[2]),
                urgent_idr=parse_idr(padded[3]),
                rptka_imta_idr=parse_idr(padded[4]),
                total_income_idr=0,
                margin_bs_idr=parse_idr(padded[5]),
                margin_bz_idr=0,
                final_price_idr=parse_idr(padded[6]),
                note=None,
            )
        )
    return out
