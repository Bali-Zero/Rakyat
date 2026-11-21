"""Pure tests of the reconciliation-lights classifier (spec L1 to L5). No database.

The oracle in this file is written independently of the classifier: it never calls the
matcher, it reasons on the DIMENSIONS an invoice is built from (amount relation, name
evidence, window, pool) and decides the light from a small table. All names are invented.
"""

from __future__ import annotations

import itertools
import random
from datetime import date

import pytest

from backend.services.accounting.lights import (
    GREEN,
    LIGHTS,
    RECORDED,
    RED,
    YELLOW,
    LightResult,
    classify_transfer,
)
from backend.services.accounting.matcher import OpenInvoice, rank_candidates

_DAY = date(2026, 6, 25)
_AMOUNT = 4_000_000
_MEMO = "Contoh Klien"

# invoice amount that stands in the given relation to a transfer of 4,000,000
_AMOUNT_OF = {
    "exact": 4_000_000,
    "net": 4_081_633,  # 4,081,633 minus its 2% withholding (81,633) is 4,000,000
    "partial": 6_000_000,  # the transfer is 2/3 of it
    "over": 1_000_000,  # the transfer exceeds it
    "none": 100_000_000,  # the transfer is under 10% of it: noise, not a partial
}
_GEN_OF = {"in": date(2026, 6, 20), "out": date(2025, 1, 1), "nodate": None}


def _build(spec: tuple[str, bool, str], invoice_id: int) -> OpenInvoice:
    relation, named, window = spec
    return OpenInvoice(
        invoice_id=invoice_id,
        practice_id=invoice_id * 10,
        client_name="Contoh Klien" if named else "Zzzz Qqqq",
        amount_idr=_AMOUNT_OF[relation],
        generated_at=_GEN_OF[window],
    )


def _classify(open_specs, rec_specs, *, amount=_AMOUNT, memo=_MEMO) -> LightResult:
    return classify_transfer(
        transfer_amount_idr=amount,
        transfer_date=_DAY,
        payer_memo=memo,
        open_invoices=[_build(s, 1 + n) for n, s in enumerate(open_specs)],
        recorded_invoices=[_build(s, 101 + n) for n, s in enumerate(rec_specs)],
    )


# ── the independent oracle ───────────────────────────────────────────────────────────────

# (strong open invoices bucketed 0/1/2+, strong recorded bucketed 0/1+) -> light, once at
# least one candidate exists in either pool
_TABLE = {
    (0, 0): YELLOW,
    (1, 0): GREEN,
    (2, 0): YELLOW,
    (0, 1): RECORDED,
    (1, 1): YELLOW,
    (2, 1): YELLOW,
}


def _tally(specs) -> tuple[int, int]:
    """(candidates, strong) of one pool, from the dimensions alone."""
    cands = [s for s in specs if s[0] in ("exact", "net")]
    strong = [s for s in cands if s[1] and s[2] != "out"]
    return len(cands), len(strong)


def _oracle(open_specs, rec_specs) -> tuple[str, tuple[int, int, int, int]]:
    oc, os_ = _tally(open_specs)
    rc, rs = _tally(rec_specs)
    counts = (oc, os_, rc, rs)
    if oc + rc == 0:
        return RED, counts
    return _TABLE[(min(os_, 2), min(rs, 1))], counts


def _oracle_suggestion(light, open_specs, rec_specs) -> int | None:
    if light == GREEN:
        return next(
            1 + n
            for n, s in enumerate(open_specs)
            if s[0] in ("exact", "net") and s[1] and s[2] != "out"
        )
    if light == RECORDED:
        strong = [
            (0 if s[0] == "exact" else 1, 101 + n)  # exact outranks net when the name agrees
            for n, s in enumerate(rec_specs)
            if s[0] in ("exact", "net") and s[1] and s[2] != "out"
        ]
        return min(strong)[1]
    return None


# 12 specs on the amounts that can be candidates, 6 on the ones that cannot
_SPECS = [
    (rel, named, win)
    for rel in ("exact", "net")
    for named in (True, False)
    for win in ("in", "out", "nodate")
] + [(rel, named, "in") for rel in ("partial", "over", "none") for named in (True, False)]


def _pools(max_size: int = 2):
    out = [()]
    for size in range(1, max_size + 1):
        out += list(itertools.combinations_with_replacement(_SPECS, size))
    return out


def test_every_pair_of_pools_up_to_two_invoices_each_gets_the_oracles_light_and_counts() -> None:
    pools = _pools()
    assert len(pools) == 1 + 18 + 171
    checked = 0
    seen = set()
    for open_specs in pools:
        for rec_specs in pools:
            light, counts = _oracle(open_specs, rec_specs)
            got = _classify(open_specs, rec_specs)
            assert got.light == light, (open_specs, rec_specs)
            assert (
                got.open_candidates,
                got.open_strong,
                got.recorded_candidates,
                got.recorded_strong,
            ) == counts, (open_specs, rec_specs)
            expected = _oracle_suggestion(light, open_specs, rec_specs)
            assert (got.suggestion.invoice_id if got.suggestion else None) == expected, (
                open_specs,
                rec_specs,
            )
            seen.add(light)
            checked += 1
    assert checked == len(pools) ** 2
    assert seen == set(LIGHTS)  # the walk reaches all four lights


# ── named edge cases ─────────────────────────────────────────────────────────────────────

_STRONG = ("exact", True, "in")
_NAMELESS = ("exact", False, "in")


def test_empty_pools_are_red_and_total() -> None:
    got = _classify([], [])
    assert (got.light, got.suggestion) == (RED, None)
    assert (got.open_candidates, got.open_strong, got.recorded_candidates, got.recorded_strong) == (
        0,
        0,
        0,
        0,
    )


def test_an_empty_pool_on_one_side_is_fine() -> None:
    assert _classify([_STRONG], []).light == GREEN
    assert _classify([], [_STRONG]).light == RECORDED


@pytest.mark.parametrize("amount", [0, -1, -4_000_000])
def test_a_zero_or_negative_amount_is_red_even_against_a_zero_amount_invoice(amount: int) -> None:
    zero = OpenInvoice(1, 10, "Contoh Klien", 0, None)
    got = classify_transfer(
        transfer_amount_idr=amount,
        transfer_date=_DAY,
        payer_memo=_MEMO,
        open_invoices=[zero, _build(_STRONG, 2)],
        recorded_invoices=[_build(_STRONG, 3)],
    )
    assert got.light == RED and got.suggestion is None


def test_an_empty_memo_gives_no_name_evidence_so_yellow_never_green() -> None:
    got = _classify([_STRONG], [], memo="")
    assert (got.light, got.open_candidates, got.open_strong) == (YELLOW, 1, 0)


def test_strong_in_both_pools_is_yellow_with_no_suggestion() -> None:
    got = _classify([_STRONG], [_STRONG])
    assert (got.light, got.open_strong, got.recorded_strong, got.suggestion) == (YELLOW, 1, 1, None)


def test_two_strong_open_is_yellow_with_no_suggestion() -> None:
    got = _classify([_STRONG, _STRONG], [])
    assert (got.light, got.open_strong, got.suggestion) == (YELLOW, 2, None)


def test_a_candidate_outside_the_window_is_yellow() -> None:
    got = _classify([("exact", True, "out")], [])
    assert (got.light, got.open_candidates, got.open_strong) == (YELLOW, 1, 0)


def test_an_invoice_with_no_date_counts_as_inside_the_window() -> None:
    assert _classify([("exact", True, "nodate")], []).light == GREEN


def test_a_net_of_pph_candidate_with_the_name_is_strong() -> None:
    got = _classify([("net", True, "in")], [])
    assert got.light == GREEN and got.suggestion.amount_relation == "net"


def test_partial_and_over_payments_are_not_candidates() -> None:
    got = _classify([("partial", True, "in"), ("over", True, "in")], [("partial", True, "in")])
    assert got.light == RED and got.open_candidates == 0 and got.recorded_candidates == 0


def test_green_ignores_extra_nameless_candidates_in_the_open_pool() -> None:
    got = _classify([_STRONG, _NAMELESS, _NAMELESS], [_NAMELESS])
    assert (got.light, got.open_candidates, got.open_strong) == (GREEN, 3, 1)
    assert got.suggestion.invoice_id == 1


def test_recorded_suggestion_is_the_highest_confidence_then_the_lowest_invoice_id() -> None:
    net, exact_a, exact_b = ("net", True, "in"), ("exact", True, "in"), ("exact", True, "in")
    got = _classify([], [net, exact_a, exact_b])
    assert got.light == RECORDED
    assert got.suggestion.invoice_id == 102  # exact beats net; of the two exact ones the lower id


def test_a_nameless_open_candidate_does_not_stop_a_strong_recorded_one() -> None:
    assert _classify([_NAMELESS], [_STRONG]).light == RECORDED  # a nameless open one is not strong


def test_the_classifier_is_pure_same_input_same_answer_and_inputs_untouched() -> None:
    open_pool = [_build(_STRONG, 1), _build(_NAMELESS, 2)]
    rec_pool = [_build(("net", True, "in"), 3)]
    before = (list(open_pool), list(rec_pool))
    first = classify_transfer(
        transfer_amount_idr=_AMOUNT,
        transfer_date=_DAY,
        payer_memo=_MEMO,
        open_invoices=open_pool,
        recorded_invoices=rec_pool,
    )
    second = classify_transfer(
        transfer_amount_idr=_AMOUNT,
        transfer_date=_DAY,
        payer_memo=_MEMO,
        open_invoices=open_pool,
        recorded_invoices=rec_pool,
    )
    assert first == second
    assert (open_pool, rec_pool) == before
    assert first.light in LIGHTS


def test_a_name_score_of_exactly_the_floor_is_evidence_and_one_point_less_is_not() -> None:
    def light(memo: str, client: str) -> str:
        return classify_transfer(
            transfer_amount_idr=_AMOUNT,
            transfer_date=_DAY,
            payer_memo=memo,
            open_invoices=[OpenInvoice(1, 10, client, _AMOUNT, None)],
            recorded_invoices=[],
        ).light

    assert light("abcde", "abcxy") == GREEN  # the matcher scores this pair at exactly 60.0
    assert light("abcdef", "abcxyz") == YELLOW  # 50.0


# ── the pre-selecting classifier equals the plain one, input for input ───────────────────


def _reference_classify(
    *, transfer_amount_idr, transfer_date, payer_memo, open_invoices, recorded_invoices
):
    """The first implementation, kept as the reference: rank the WHOLE pool, filter afterwards."""

    def cands(pool):
        if not pool:
            return []
        ranked = rank_candidates(
            transfer_amount_idr=transfer_amount_idr,
            transfer_date=transfer_date,
            payer_memo=payer_memo,
            open_invoices=pool,
            limit=len(pool),
        )
        return [c for c in ranked if c.amount_relation in ("exact", "net")]

    oc, rc = cands(open_invoices), cands(recorded_invoices)
    os_ = [c for c in oc if c.name_score >= 60.0 and c.in_window]
    rs = [c for c in rc if c.name_score >= 60.0 and c.in_window]
    suggestion = None
    if not oc and not rc:
        light = RED
    elif len(os_) == 1 and not rs:
        light, suggestion = GREEN, os_[0]
    elif rs and not os_:
        light, suggestion = RECORDED, sorted(rs, key=lambda c: (-c.confidence, c.invoice_id))[0]
    else:
        light = YELLOW
    return LightResult(light, len(oc), len(os_), len(rc), len(rs), suggestion)


_GEN_NAMES = [
    "Contoh Klien",
    "Contoh Maju",
    "Sampel Jaya Abadi",
    "Uji Fiktif",
    "Klien Contoh",
    "Zzzz Qqqq",
    "",
]
_GEN_PRICES = [
    1_000_000,
    2_000_000,
    4_000_000,
    4_081_633,
    4_000_500,
    6_000_000,
    8_163_265,
    40_000_000,
    0,
    -5,
]


def _gen_invoice(rng: random.Random, invoice_id: int) -> OpenInvoice:
    amount = rng.choice(_GEN_PRICES)
    return OpenInvoice(
        invoice_id=invoice_id,
        practice_id=invoice_id * 10,
        client_name=rng.choice(_GEN_NAMES),
        amount_idr=amount,
        generated_at=rng.choice([None, date(2026, 6, 20), date(2026, 5, 1), date(2025, 1, 1)]),
        pph_expected_idr=rng.choice([0, 0, 80_000, 163_265]),
    )


def test_the_preselecting_classifier_returns_exactly_the_reference_result_on_generated_inputs() -> (
    None
):
    rng = random.Random(20261118)
    checked = 0
    lights_seen = set()
    for _ in range(4000):
        open_pool = [_gen_invoice(rng, 1 + n) for n in range(rng.randint(0, 6))]
        rec_pool = [_gen_invoice(rng, 101 + n) for n in range(rng.randint(0, 6))]
        kwargs = {
            "transfer_amount_idr": rng.choice(_GEN_PRICES + [3_920_000, 7_999_999]),
            "transfer_date": _DAY,
            "payer_memo": rng.choice(_GEN_NAMES + ["TRF CONTOH KLIEN PT"]),
            "open_invoices": open_pool,
            "recorded_invoices": rec_pool,
        }
        got = classify_transfer(**kwargs)
        assert got == _reference_classify(**kwargs), kwargs  # light, counts, every suggestion field
        lights_seen.add(got.light)
        checked += 1
    assert checked == 4000 and lights_seen == set(LIGHTS)


# ── boundaries the matcher's rules draw (each pinned from both sides) ────────────────────


def _one(
    *, amount=_AMOUNT, memo=_MEMO, invoice_amount=_AMOUNT, name="Contoh Klien", gen=None, pph=0
):
    return classify_transfer(
        transfer_amount_idr=amount,
        transfer_date=_DAY,
        payer_memo=memo,
        open_invoices=[OpenInvoice(1, 10, name, invoice_amount, gen, pph)],
        recorded_invoices=[],
    )


def test_the_window_is_60_days_both_ends_included() -> None:
    from datetime import timedelta

    assert _one(gen=_DAY - timedelta(days=60)).light == GREEN
    assert _one(gen=_DAY + timedelta(days=60)).light == GREEN
    assert _one(gen=_DAY - timedelta(days=61)).light == YELLOW
    assert _one(gen=_DAY + timedelta(days=61)).light == YELLOW


def test_the_name_floor_is_applied_to_the_raw_score_without_rounding() -> None:
    near_miss = ("a" * 72 + "b" * 49, "a" * 72 + "c" * 49)  # scores 59.504: rounds to 60, is not 60
    got = _one(memo=near_miss[0], name=near_miss[1])
    assert (got.light, got.open_candidates, got.open_strong) == (YELLOW, 1, 0)
    assert _one(memo="abcde", name="abcxy").light == GREEN  # exactly 60.0


def test_the_exact_tolerance_is_1000_inside_and_1001_outside() -> None:
    assert _one(amount=_AMOUNT + 1_000).open_candidates == 1
    assert _one(amount=_AMOUNT - 1_000).open_candidates == 1
    assert _one(amount=_AMOUNT + 1_001).open_candidates == 0
    assert _one(amount=_AMOUNT - 1_001).open_candidates == 0  # a partial payment, not a candidate
    # the same two edges around the net-of-PPh amount (4,081,633 less 81,633 is 4,000,000)
    assert _one(invoice_amount=4_081_633 + 1_000).open_candidates == 1
    assert _one(invoice_amount=4_081_633 + 1_300).open_candidates == 0


def test_the_classifier_does_not_modify_what_it_is_given() -> None:
    import copy

    open_pool = [_build(_STRONG, 1), _build(("net", True, "out"), 2), _build(_NAMELESS, 3)]
    rec_pool = [_build(("net", True, "nodate"), 4)]
    snapshot = copy.deepcopy((open_pool, rec_pool))
    classify_transfer(
        transfer_amount_idr=_AMOUNT,
        transfer_date=_DAY,
        payer_memo=_MEMO,
        open_invoices=open_pool,
        recorded_invoices=rec_pool,
    )
    assert (open_pool, rec_pool) == snapshot
    assert [type(x) for x in open_pool] == [OpenInvoice] * 3


@pytest.mark.parametrize("big", [2**63 - 1, -(2**63), 2**63 - 2, 1])
def test_it_works_at_the_64_bit_limits(big: int) -> None:
    for transfer, invoice in (
        (big, big),
        (big, 4_000_000),
        (4_000_000, big),
        (big, -big if big > 0 else 0),
    ):
        got = classify_transfer(
            transfer_amount_idr=transfer,
            transfer_date=_DAY,
            payer_memo=_MEMO,
            open_invoices=[OpenInvoice(1, 10, "Contoh Klien", invoice, date(2026, 6, 1), invoice)],
            recorded_invoices=[OpenInvoice(2, 20, "Contoh Klien", invoice, None, 0)],
        )
        assert got.light in LIGHTS
    exact = classify_transfer(
        transfer_amount_idr=2**63 - 1,
        transfer_date=_DAY,
        payer_memo=_MEMO,
        open_invoices=[OpenInvoice(1, 10, "Contoh Klien", 2**63 - 1, None)],
        recorded_invoices=[],
    )
    assert exact.light == GREEN
