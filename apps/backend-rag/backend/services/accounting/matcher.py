"""Reconciliation matcher — propose invoice candidates for an incoming transfer.

Pure logic, no DB: given one bank credit and a list of open invoices, rank
candidate matches. The accountant always makes the final call (manual confirm).

Panel decision (Gemini+DeepSeek+Codex unanimous): NO subset-sum. Subset-sum
"generates plausible lies" with PPh23 withholding, bank fees, and partial
payments. Instead:
  - exact amount (± tolerance) within a date window  -> high confidence
  - amount net of expected PPh23 (gross - 2%)         -> "likely withheld" hint
  - rapidfuzz on payer name vs client name            -> name hint (never auto)
Multi-invoice payments: the accountant manually multi-selects (deterministic),
handled at the service layer — NOT guessed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

# Default Indonesian B2B PPh23 withholding rate (2%).
PPH23_RATE = 0.02
# Amount tolerance for "exact" match (rounding / sub-rupiah noise), in IDR.
EXACT_TOLERANCE_IDR = 1_000
# How many days around the transfer date an invoice is still a plausible match.
DATE_WINDOW_DAYS = 60
# rapidfuzz name score (0-100) below which we don't even surface a name hint.
NAME_HINT_FLOOR = 60.0
# A "partial payment" smaller than this fraction of the invoice is noise, not a
# plausible installment — don't surface it (avoids spamming the review screen
# with a tiny transfer "matching" every large open invoice).
MIN_PARTIAL_FRACTION = 0.10


@dataclass(frozen=True)
class OpenInvoice:
    """The subset of invoice/practice fields the matcher needs."""

    invoice_id: int
    practice_id: int
    client_name: str
    amount_idr: int
    generated_at: date | None = None
    pph_expected_idr: int = 0
    # Identity of the invoice for a human; None when the source has no value.
    invoice_number: str | None = None
    invoice_issued_at: str | None = None
    service_name: str | None = None
    # The stored issuer; NULL on invoices made before issuer routing (the default company's).
    issuer_entity_key: str | None = None
    bill_to_company: str | None = None


@dataclass
class MatchCandidate:
    """A ranked candidate match for a bank transfer."""

    invoice_id: int
    practice_id: int
    client_name: str
    invoice_amount_idr: int
    confidence: float  # 0.0 - 1.0
    reasons: list[str] = field(default_factory=list)
    amount_delta_idr: int = 0  # transfer - invoice (negative = transfer is less)
    likely_withheld: bool = False
    # Structured copies of the three rules above, read by the lights classifier so it
    # cannot drift from them. Defaults keep every existing constructor call valid.
    amount_relation: str = "none"  # exact | net | partial | over | none
    name_score: float = 0.0  # 0-100, the raw fuzzy score (also below the hint floor)
    in_window: bool = True
    invoice_number: str | None = None
    invoice_issued_at: str | None = None
    service_name: str | None = None


def _amount_signals(transfer_idr: int, inv: OpenInvoice) -> tuple[float, list[str], int, bool, str]:
    """Score the amount relationship. Returns (score, reasons, delta, withheld, relation)."""
    reasons: list[str] = []
    delta = transfer_idr - inv.amount_idr

    # D6 guard: a zero/non-positive transfer or zero-amount invoice can never be
    # a real payment match — never let 0==0 phantom-match as "exact".
    if transfer_idr <= 0 or inv.amount_idr <= 0:
        return 0.0, reasons, delta, False, "none"

    # Exact (within tolerance)
    if abs(delta) <= EXACT_TOLERANCE_IDR:
        reasons.append("exact amount match")
        return 0.7, reasons, delta, False, "exact"

    # Net of expected PPh23: a B2B client transfers gross - 2% (or the
    # explicitly-recorded pph_expected). transfer ~= amount - withholding.
    expected_wh = inv.pph_expected_idr or round(inv.amount_idr * PPH23_RATE)
    net_expected = inv.amount_idr - expected_wh
    if abs(transfer_idr - net_expected) <= EXACT_TOLERANCE_IDR:
        reasons.append(f"matches amount net of PPh23 (withheld ~{expected_wh:,} IDR)")
        return 0.6, reasons, delta, True, "net"

    # Partial payment: transfer is a meaningful fraction less than the invoice.
    # Below MIN_PARTIAL_FRACTION it's noise, not a plausible installment.
    if inv.amount_idr * MIN_PARTIAL_FRACTION <= transfer_idr < inv.amount_idr:
        reasons.append("possible partial payment (transfer < invoice)")
        return 0.2, reasons, delta, False, "partial"

    # Overpayment / lump sum that exceeds this single invoice.
    if transfer_idr > inv.amount_idr + EXACT_TOLERANCE_IDR:
        reasons.append("transfer exceeds this invoice (overpayment or covers several)")
        return 0.15, reasons, delta, False, "over"

    return 0.0, reasons, delta, False, "none"


def _date_in_window(transfer_date: date, inv: OpenInvoice) -> bool:
    if inv.generated_at is None:
        return True  # unknown -> don't penalize
    return abs((transfer_date - inv.generated_at).days) <= DATE_WINDOW_DAYS


_NAME = r"([A-Za-z][A-Za-z .'-]*?)"
_PERMATA_NAME = r"([A-Za-z0-9][A-Za-z0-9 .&'/-]*?)"
_PERMATA_PATTERNS = (
    re.compile(r"^TRF DARI\s+" + _PERMATA_NAME + r"\s+BANK\s+.+?\s+ATM\b", re.I),
    re.compile(r"^TRF INCOMING BIFAST DARI\s+" + _PERMATA_NAME + r"\s+\d{6,}\s+BANK\b", re.I),
    re.compile(
        r"^PB DARI\s+" + _PERMATA_NAME + r"\s+(?:NEW PEB|PERMATA\s*ME)\s*\d{2}:\d{2}:\d{2}\b", re.I
    ),
)
_PAYER_PATTERNS = (
    *_PERMATA_PATTERNS,
    re.compile(r"\bFR\s+[A-Za-z]{4}IDJA\s+UN\s+(.+)$"),
    re.compile(r"\bTRF FR\s+" + _NAME + r"\s+\d{8,}\b"),
    re.compile(r"\bINCOMING SKN\s+\S+/[^/]+/" + _NAME + r"\s+[A-Za-z]?\d{6,}"),
    re.compile(r"\bINCOMING SWIFT\s+\d+\s+" + _NAME + r"\s*/"),
)
_INVOICE_NUMBER = re.compile(r"INV-\d{6}-\d{5}(?!\d)")
# Score of a payer whose every word matches a client word, with at least two matched.
_FULL_NAME_SCORE = 97.0


def extract_payer_name(description: str) -> str:
    """The payer name inside a bank description line, or "" when it carries none."""
    for pattern in _PAYER_PATTERNS:
        found = pattern.search(description or "")
        if found:
            name = " ".join(found.group(1).split())
            if re.search(r"[A-Za-z]{2}", name):
                return name
    return ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _company_name_tokens(name: str) -> tuple[str, list[str]]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    if words and words[0] in {"pt", "cv"}:
        return words[0], words[1:]
    if words and words[-1] in {"pt", "cv"}:
        return words[-1], words[:-1]
    return "", words


def is_permata_transfer(memo: str) -> bool:
    return any(pattern.match(memo or "") for pattern in _PERMATA_PATTERNS)


def _name_score(payer_memo: str, client_name: str) -> float:
    """0-100 fuzzy score between the bank memo and the client name."""
    if not payer_memo or not client_name:
        return 0.0
    payer = extract_payer_name(payer_memo)
    if not payer:
        # token_set_ratio is robust to word order / extra tokens in bank memos.
        return float(fuzz.token_set_ratio(payer_memo.lower(), client_name.lower()))
    payer_words, client_words = _words(payer), _words(client_name)
    free = list(client_words)
    matched = 0
    for i, word in enumerate(payer_words):
        # The bank cuts the name at 14 characters: the last word may be a prefix.
        cut = i == len(payer_words) - 1 and len(word) >= 3
        hit = next((c for c in free if c == word or (cut and c.startswith(word))), None)
        if hit is not None:
            free.remove(hit)
            matched += 1
    if matched == len(payer_words) and matched >= 2:
        return _FULL_NAME_SCORE
    # token_sort (not set): a payer that is a subset of the client name earns no free 100.
    score = float(fuzz.token_sort_ratio(" ".join(payer_words), " ".join(client_words)))
    if len(payer_words) < 2 or len(client_words) < 2:
        # one word on either side (a client saved by first name only) is never evidence alone
        score = min(score, NAME_HINT_FLOOR - 1)
    return score


def rank_candidates(
    *,
    transfer_amount_idr: int,
    transfer_date: date,
    payer_memo: str,
    open_invoices: list[OpenInvoice],
    limit: int = 5,
) -> list[MatchCandidate]:
    """Rank open invoices as candidate matches for one incoming transfer.

    Confidence blends amount signal (primary) with name similarity (secondary)
    and a date-window gate. Returns the top `limit`, highest confidence first.
    Empty list means nothing plausible — the transfer stays 'unmatched'.
    """
    candidates: list[MatchCandidate] = []
    payer = extract_payer_name(payer_memo)
    payer_prefix, company_words = _company_name_tokens(payer)
    permata = is_permata_transfer(payer_memo)

    for inv in open_invoices:
        amount_score, reasons, delta, withheld, relation = _amount_signals(transfer_amount_idr, inv)
        if amount_score <= 0.0:
            continue  # no amount relationship at all -> not a candidate

        name = _name_score(payer_memo, inv.client_name)
        bill_prefix, bill_words = _company_name_tokens(inv.bill_to_company or "")
        company_match = (
            len(company_words) >= 2
            and company_words == bill_words
            and (not payer_prefix or not bill_prefix or payer_prefix == bill_prefix)
        )
        if company_match:
            name = 100.0
        number_hit = bool(inv.invoice_number) and inv.invoice_number in _INVOICE_NUMBER.findall(
            payer_memo or ""
        )
        if number_hit:
            name = 100.0  # our own invoice number in the memo is stronger than any name
        in_window = _date_in_window(transfer_date, inv)
        later_invoice = (
            permata
            and not number_hit
            and inv.generated_at is not None
            and transfer_date < inv.generated_at
        )

        # Blend: amount is primary (up to 0.7), name adds up to 0.25, date gates.
        confidence = amount_score
        if number_hit:
            confidence += 0.25
            reasons.append("invoice number in bank memo")
        elif company_match:
            confidence += 0.25
            reasons.append("payer matches the company on the invoice")
        elif name >= NAME_HINT_FLOOR:
            confidence += (name / 100.0) * 0.25
            reasons.append(f"payer name ~ client ({name:.0f}%)")
        if later_invoice:
            in_window = False
            confidence *= 0.5
            reasons.append("invoice issued after the bank transfer")
        elif not in_window:
            confidence *= 0.5
            reasons.append("outside 60-day date window")

        confidence = round(min(confidence, 1.0), 3)
        candidates.append(
            MatchCandidate(
                invoice_id=inv.invoice_id,
                practice_id=inv.practice_id,
                client_name=(inv.bill_to_company if company_match else None) or inv.client_name,
                invoice_amount_idr=inv.amount_idr,
                confidence=confidence,
                reasons=reasons,
                amount_delta_idr=delta,
                likely_withheld=withheld,
                amount_relation=relation,
                name_score=name,
                in_window=in_window,
                invoice_number=inv.invoice_number,
                invoice_issued_at=inv.invoice_issued_at,
                service_name=inv.service_name,
            )
        )

    # Equal confidence: the invoice whose amount is closest to the transfer first. Without it a
    # weak signal (partial, over) kept the pool's order, so one large undated invoice headed the
    # list of every smaller transfer.
    candidates.sort(key=lambda c: (-c.confidence, abs(c.amount_delta_idr)))
    return candidates[:limit]
