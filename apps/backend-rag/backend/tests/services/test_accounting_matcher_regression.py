"""Matcher regression: confidence, order and reasons are pinned as literals.

The expected values below were computed from the matcher as it was BEFORE the structured
fields (amount_relation, name_score, in_window) were added. Those fields are additive; the
ranking itself must not move, with one deliberate change (2026-11-21): candidates of EQUAL
confidence now come closest amount first instead of in the pool's order. The pinned lists keep
their old order and the test applies that one rule to them, so every confidence, reason and
delta stays pinned as it was. All names are invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.services.accounting.matcher import MatchCandidate, OpenInvoice, rank_candidates

_DAY = date(2026, 6, 25)


def _inv(i: int, amount: int, name: str, gen: date | None = None, pph: int = 0) -> OpenInvoice:
    return OpenInvoice(i, i * 10, name, amount, gen, pph)


_INVOICES = [
    _inv(1, 4_000_000, "Contoh Klien"),
    _inv(2, 4_000_000, "Lain Orang"),
    _inv(3, 4_000_500, "Contoh Klien Satu"),
    _inv(4, 4_081_633, "PT Contoh Maju"),
    _inv(5, 4_000_000, "Contoh Klien", date(2026, 1, 1)),
    _inv(6, 10_000_000, "Contoh Klien"),
    _inv(7, 1_000_000, "Contoh Klien"),
    _inv(8, 0, "Nol"),
    _inv(9, 4_200_000, "Contoh Klien", None, 160_000),
    _inv(10, 3_000_000, "Contoh Klien", date(2026, 6, 1)),
]

# (transfer, memo, [(invoice_id, confidence, reasons, amount_delta_idr, likely_withheld), ...])
_PINNED = [
    (
        4000000,
        "CONTOH KLIEN",
        [
            (1, 0.95, ["exact amount match", "payer name ~ client (100%)"], 0, False),
            (3, 0.95, ["exact amount match", "payer name ~ client (100%)"], -500, False),
            (
                4,
                0.767,
                ["matches amount net of PPh23 (withheld ~81,633 IDR)", "payer name ~ client (67%)"],
                -81633,
                True,
            ),
            (2, 0.7, ["exact amount match"], 0, False),
            (
                5,
                0.475,
                ["exact amount match", "payer name ~ client (100%)", "outside 60-day date window"],
                0,
                False,
            ),
            (
                6,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -6000000,
                False,
            ),
            (
                9,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -200000,
                False,
            ),
            (
                7,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                3000000,
                False,
            ),
            (
                10,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                1000000,
                False,
            ),
        ],
    ),
    (
        4000000,
        "",
        [
            (1, 0.7, ["exact amount match"], 0, False),
            (2, 0.7, ["exact amount match"], 0, False),
            (3, 0.7, ["exact amount match"], -500, False),
            (4, 0.6, ["matches amount net of PPh23 (withheld ~81,633 IDR)"], -81633, True),
            (5, 0.35, ["exact amount match", "outside 60-day date window"], 0, False),
            (6, 0.2, ["possible partial payment (transfer < invoice)"], -6000000, False),
            (9, 0.2, ["possible partial payment (transfer < invoice)"], -200000, False),
            (
                7,
                0.15,
                ["transfer exceeds this invoice (overpayment or covers several)"],
                3000000,
                False,
            ),
            (
                10,
                0.15,
                ["transfer exceeds this invoice (overpayment or covers several)"],
                1000000,
                False,
            ),
        ],
    ),
    (
        3920000,
        "transfer PT Contoh Maju",
        [
            (
                1,
                0.767,
                ["matches amount net of PPh23 (withheld ~80,000 IDR)", "payer name ~ client (67%)"],
                -80000,
                True,
            ),
            (2, 0.6, ["matches amount net of PPh23 (withheld ~80,000 IDR)"], -80000, True),
            (3, 0.6, ["matches amount net of PPh23 (withheld ~80,010 IDR)"], -80500, True),
            (
                4,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -161633,
                False,
            ),
            (
                5,
                0.383,
                [
                    "matches amount net of PPh23 (withheld ~80,000 IDR)",
                    "payer name ~ client (67%)",
                    "outside 60-day date window",
                ],
                -80000,
                True,
            ),
            (
                6,
                0.367,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (67%)"],
                -6080000,
                False,
            ),
            (
                9,
                0.367,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (67%)"],
                -280000,
                False,
            ),
            (
                7,
                0.317,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (67%)",
                ],
                2920000,
                False,
            ),
            (
                10,
                0.317,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (67%)",
                ],
                920000,
                False,
            ),
        ],
    ),
    (0, "x", []),
    (-5, "x", []),
    (
        500000,
        "Contoh Klien",
        [
            (
                1,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -3500000,
                False,
            ),
            (
                3,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -3500500,
                False,
            ),
            (
                7,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -500000,
                False,
            ),
            (
                9,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -3700000,
                False,
            ),
            (
                10,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -2500000,
                False,
            ),
            (
                4,
                0.367,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (67%)"],
                -3581633,
                False,
            ),
            (
                5,
                0.225,
                [
                    "possible partial payment (transfer < invoice)",
                    "payer name ~ client (100%)",
                    "outside 60-day date window",
                ],
                -3500000,
                False,
            ),
            (2, 0.2, ["possible partial payment (transfer < invoice)"], -3500000, False),
        ],
    ),
    (
        20000000,
        "Contoh Klien",
        [
            (
                1,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                16000000,
                False,
            ),
            (
                3,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                15999500,
                False,
            ),
            (
                6,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                10000000,
                False,
            ),
            (
                7,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                19000000,
                False,
            ),
            (
                9,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                15800000,
                False,
            ),
            (
                10,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                17000000,
                False,
            ),
            (
                4,
                0.317,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (67%)",
                ],
                15918367,
                False,
            ),
            (
                5,
                0.2,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                    "outside 60-day date window",
                ],
                16000000,
                False,
            ),
            (
                2,
                0.15,
                ["transfer exceeds this invoice (overpayment or covers several)"],
                16000000,
                False,
            ),
        ],
    ),
    (
        3840000,
        "contoh klien",
        [
            (
                1,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -160000,
                False,
            ),
            (
                3,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -160500,
                False,
            ),
            (
                6,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -6160000,
                False,
            ),
            (
                9,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -360000,
                False,
            ),
            (
                7,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                2840000,
                False,
            ),
            (
                10,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                840000,
                False,
            ),
            (
                4,
                0.367,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (67%)"],
                -241633,
                False,
            ),
            (
                5,
                0.225,
                [
                    "possible partial payment (transfer < invoice)",
                    "payer name ~ client (100%)",
                    "outside 60-day date window",
                ],
                -160000,
                False,
            ),
            (2, 0.2, ["possible partial payment (transfer < invoice)"], -160000, False),
        ],
    ),
    (
        3000000,
        "klien contoh",
        [
            (10, 0.95, ["exact amount match", "payer name ~ client (100%)"], 0, False),
            (
                1,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -1000000,
                False,
            ),
            (
                3,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -1000500,
                False,
            ),
            (
                6,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -7000000,
                False,
            ),
            (
                9,
                0.45,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (100%)"],
                -1200000,
                False,
            ),
            (
                7,
                0.4,
                [
                    "transfer exceeds this invoice (overpayment or covers several)",
                    "payer name ~ client (100%)",
                ],
                2000000,
                False,
            ),
            (
                4,
                0.367,
                ["possible partial payment (transfer < invoice)", "payer name ~ client (67%)"],
                -1081633,
                False,
            ),
            (
                5,
                0.225,
                [
                    "possible partial payment (transfer < invoice)",
                    "payer name ~ client (100%)",
                    "outside 60-day date window",
                ],
                -1000000,
                False,
            ),
            (2, 0.2, ["possible partial payment (transfer < invoice)"], -1000000, False),
        ],
    ),
]


@pytest.mark.parametrize(("amount", "memo", "expected"), _PINNED)
def test_ranking_is_exactly_what_it_was_before_the_structured_fields(
    amount, memo, expected
) -> None:
    out = rank_candidates(
        transfer_amount_idr=amount,
        transfer_date=_DAY,
        payer_memo=memo,
        open_invoices=_INVOICES,
        limit=50,
    )
    got = [
        (c.invoice_id, c.confidence, c.reasons, c.amount_delta_idr, c.likely_withheld) for c in out
    ]
    # The old order, re-ranked only inside equal confidence: closest amount first (stable).
    assert got == sorted(expected, key=lambda e: (-e[1], abs(e[3])))


def test_the_tie_rule_moves_only_ties() -> None:
    """Every pinned case keeps its confidences in the same non-increasing sequence."""
    for amount, memo, expected in _PINNED:
        out = rank_candidates(
            transfer_amount_idr=amount,
            transfer_date=_DAY,
            payer_memo=memo,
            open_invoices=_INVOICES,
            limit=50,
        )
        assert [c.confidence for c in out] == [e[1] for e in expected]
        assert sorted(c.invoice_id for c in out) == sorted(e[0] for e in expected)


def test_default_limit_is_still_five() -> None:
    out = rank_candidates(
        transfer_amount_idr=4_000_000, transfer_date=_DAY, payer_memo="", open_invoices=_INVOICES
    )
    assert [c.invoice_id for c in out] == [1, 2, 3, 4, 5]


def test_a_candidate_built_the_old_way_still_works_and_carries_the_defaults() -> None:
    c = MatchCandidate(1, 2, "Contoh Klien", 100, 0.5)
    assert (c.amount_relation, c.name_score, c.in_window) == ("none", 0.0, True)


@pytest.mark.parametrize(
    ("amount", "relation"),
    [(4_000_000, "exact"), (3_920_000, "net"), (2_000_000, "partial"), (9_000_000, "over")],
)
def test_the_structured_relation_names_the_rule_that_scored(amount: int, relation: str) -> None:
    out = rank_candidates(
        transfer_amount_idr=amount,
        transfer_date=_DAY,
        payer_memo="Contoh Klien",
        open_invoices=[_inv(1, 4_000_000, "Contoh Klien")],
    )
    assert out[0].amount_relation == relation
    assert out[0].name_score == 100.0 and out[0].in_window is True


def test_structured_window_and_name_score_are_the_matchers_own() -> None:
    out = rank_candidates(
        transfer_amount_idr=4_000_000,
        transfer_date=_DAY,
        payer_memo="zzz",
        open_invoices=[_inv(5, 4_000_000, "Contoh Klien", date(2026, 1, 1))],
    )
    assert out[0].in_window is False and out[0].name_score < 60.0


def test_the_exact_tolerance_is_1000_inside_and_1001_outside() -> None:
    def relation(transfer: int) -> str | None:
        out = rank_candidates(
            transfer_amount_idr=transfer,
            transfer_date=_DAY,
            payer_memo="",
            open_invoices=[_inv(1, 4_000_000, "Contoh Klien")],
        )
        return out[0].amount_relation if out else None

    assert relation(4_001_000) == "exact" and relation(3_999_000) == "exact"
    assert relation(4_001_001) == "over" and relation(3_998_999) == "partial"
    # and around the net-of-PPh amount (3,920,000)
    assert relation(3_921_000) == "net" and relation(3_919_000) == "net"
    assert relation(3_921_001) == "partial" and relation(3_918_999) == "partial"


def test_the_window_is_60_days_inclusive() -> None:
    from datetime import timedelta

    def in_window(days: int) -> bool:
        out = rank_candidates(
            transfer_amount_idr=4_000_000,
            transfer_date=_DAY,
            payer_memo="",
            open_invoices=[_inv(1, 4_000_000, "X", _DAY - timedelta(days=days))],
        )
        return out[0].in_window

    assert in_window(60) and not in_window(61)
