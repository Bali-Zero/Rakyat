"""Reconciliation lights — advice for one unmatched incoming transfer.

Pure logic, no database, no clock, no randomness (spec L1). The classifier looks at both
invoice pools at once, the OPEN pool (practice not yet paid) and the RECORDED pool (practice
already marked paid), and gives the transfer exactly one of four lights:

  green     exactly one STRONG open invoice and no STRONG recorded one
  recorded  at least one STRONG recorded invoice and no STRONG open one
  red       no invoice of either pool is a CANDIDATE
  yellow    every other case

A CANDIDATE is an invoice whose amount agrees with the transfer exactly or net of PPh23; a
STRONG one is a candidate with payer-name evidence, inside the date window. All three rules
are the matcher's own, read from the structured fields it puts on its candidates, so the two
cannot drift apart. A light is advice: nothing here confirms, links or ignores anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from backend.services.accounting.matcher import (
    NAME_HINT_FLOOR,
    MatchCandidate,
    OpenInvoice,
    _amount_signals,
    rank_candidates,
)

GREEN = "green"
RECORDED = "recorded"
YELLOW = "yellow"
RED = "red"
LIGHTS = (GREEN, RECORDED, YELLOW, RED)

_CANDIDATE_RELATIONS = ("exact", "net")


@dataclass(frozen=True)
class LightResult:
    light: str
    open_candidates: int
    open_strong: int
    recorded_candidates: int
    recorded_strong: int
    suggestion: MatchCandidate | None


def _candidates(
    pool: list[OpenInvoice], *, amount: int, day: date, memo: str
) -> list[MatchCandidate]:
    """Every invoice of the pool whose amount is exact or net (the matcher's own rules).

    The matcher's amount rule runs first, alone, so the fuzzy name comparison is paid only
    for invoices that can be candidates; ranking a subset ranks each member exactly as
    ranking the whole pool would.
    """
    fits = [inv for inv in pool if _amount_signals(amount, inv)[4] in _CANDIDATE_RELATIONS]
    if not fits:
        return []
    return rank_candidates(
        transfer_amount_idr=amount,
        transfer_date=day,
        payer_memo=memo,
        open_invoices=fits,
        limit=len(fits),
    )


def _is_strong(c: MatchCandidate) -> bool:
    return c.name_score >= NAME_HINT_FLOOR and c.in_window


def classify_transfer(
    *,
    transfer_amount_idr: int,
    transfer_date: date,
    payer_memo: str,
    open_invoices: list[OpenInvoice],
    recorded_invoices: list[OpenInvoice],
) -> LightResult:
    """One light, four counts and the suggested invoice (if any) for one credit."""
    open_c = _candidates(
        open_invoices, amount=transfer_amount_idr, day=transfer_date, memo=payer_memo
    )
    rec_c = _candidates(
        recorded_invoices, amount=transfer_amount_idr, day=transfer_date, memo=payer_memo
    )
    open_s = [c for c in open_c if _is_strong(c)]
    rec_s = [c for c in rec_c if _is_strong(c)]

    suggestion: MatchCandidate | None = None
    if not open_c and not rec_c:
        light = RED
    elif len(open_s) == 1 and not rec_s:
        light = GREEN
        suggestion = open_s[0]
    elif rec_s and not open_s:
        light = RECORDED
        suggestion = sorted(rec_s, key=lambda c: (-c.confidence, c.invoice_id))[0]
    else:
        light = YELLOW

    return LightResult(
        light=light,
        open_candidates=len(open_c),
        open_strong=len(open_s),
        recorded_candidates=len(rec_c),
        recorded_strong=len(rec_s),
        suggestion=suggestion,
    )
