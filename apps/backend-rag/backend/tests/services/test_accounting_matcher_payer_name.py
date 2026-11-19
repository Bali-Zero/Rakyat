"""Payer-name extraction and invoice-number evidence in the reconciliation matcher (synthetic data)."""

from __future__ import annotations

from datetime import date

import pytest
from rapidfuzz import fuzz

from backend.services.accounting.lights import GREEN, YELLOW, classify_transfer
from backend.services.accounting.matcher import (
    NAME_HINT_FLOOR,
    OpenInvoice,
    _name_score,
    extract_payer_name,
    rank_candidates,
)

CIMB = "20261104BBBAIDJA010O0290704618 BFS        FR BBBAIDJA UN MARIO ROSSINELL"
ATM = "ATM Prima TRF FR MARIO ROSSINELLI 9999999999979803 BANK PERMATA TRANSFER RINTIS"
SKN = "INCOMING SKN NISPIDJA/AP. 1/MARIO ROSSINELLI K036404079240 /PURP/IVPT///ROC/x"
SWIFT = "INCOMING SWIFT 2026111300218668 MARIO ROSSINELLI /ROC/ref/URI/x"
GATEWAY = "20261102SAHMIDJA010O0223071847 INV-202611-01382 BFS FR SAHMIDJA UN INV-202611-013"
DAY = date(2026, 11, 4)
REASON = "invoice number in bank memo"


def _inv(i: int, name: str, number: str | None = None, amount: int = 5_000_000) -> OpenInvoice:
    return OpenInvoice(
        invoice_id=i, practice_id=i * 10, client_name=name, amount_idr=amount, invoice_number=number
    )


def _classify(memo: str, inv: OpenInvoice):
    return classify_transfer(
        transfer_amount_idr=5_000_000,
        transfer_date=DAY,
        payer_memo=memo,
        open_invoices=[inv],
        recorded_invoices=[],
    )


@pytest.mark.parametrize(
    "line, expected",
    [
        (CIMB, "MARIO ROSSINELL"),
        (ATM, "MARIO ROSSINELLI"),
        (SKN, "MARIO ROSSINELLI"),
        (SWIFT, "MARIO ROSSINELLI"),
        ("AtmBersama TRF FR 9999999999999999 BRI", ""),
        ("API TRF FR 803777000900 API_TXN ref", ""),
        ("", ""),
    ],
)
def test_extract_payer_name_per_bank_line_shape(line: str, expected: str) -> None:
    assert extract_payer_name(line) == expected


def test_a_cut_surname_reaches_the_floor_where_the_whole_line_did_not() -> None:
    assert fuzz.token_set_ratio(CIMB.lower(), "mario rossinelli") < NAME_HINT_FLOOR
    assert _name_score(CIMB, "Mario Rossinelli") >= NAME_HINT_FLOOR


def test_word_order_is_free() -> None:
    assert _name_score(CIMB, "Rossinelli Mario") >= NAME_HINT_FLOOR


def test_a_lone_first_name_client_stays_below_the_floor() -> None:
    assert _name_score(CIMB, "Mario") < NAME_HINT_FLOOR


def test_a_different_person_with_the_same_first_name_stays_below_the_floor() -> None:
    assert _name_score(CIMB, "Mario Bianchi") < NAME_HINT_FLOOR


def test_a_misspelt_surname_on_two_words_still_reaches_the_floor() -> None:
    line = "20261028SAHMIDJA010O0222355481 BFS FR SAHMIDJA UN Paolo Brambila"
    assert _name_score(line, "Paolo Brambilla") >= NAME_HINT_FLOOR


def test_a_two_word_payer_against_a_one_word_client_stays_below_the_floor() -> None:
    line = "20261109BBBAIDJA010O0290704618 BFS FR BBBAIDJA UN GIULIANA BERTOL"
    assert _name_score(line, "Giuliana") < NAME_HINT_FLOOR


def test_a_line_without_a_name_keeps_the_whole_line_score() -> None:
    line = "AtmBersama TRF FR 9999999999999999 BRI Mario Rossinelli"
    old = float(fuzz.token_set_ratio(line.lower(), "mario rossinelli"))
    assert old >= NAME_HINT_FLOOR
    assert _name_score(line, "Mario Rossinelli") == old


def test_invoice_number_exact_makes_the_only_candidate_green() -> None:
    res = _classify(GATEWAY, _inv(1, "Unrelated Client", number="INV-202611-01382"))
    assert res.light == GREEN
    assert REASON in res.suggestion.reasons


def test_a_truncated_invoice_number_matches_nothing() -> None:
    inv = _inv(1, "Unrelated Client", number="INV-202611-01382")
    memo = "20261102SAHMIDJA010O0223071847 BFS FR SAHMIDJA UN INV-202611-013"
    assert _classify(memo, inv).light == YELLOW
    (c,) = rank_candidates(
        transfer_amount_idr=5_000_000, transfer_date=DAY, payer_memo=memo, open_invoices=[inv]
    )
    assert REASON not in c.reasons


def test_the_invoice_number_of_another_invoice_gives_no_evidence() -> None:
    inv = _inv(1, "Unrelated Client", number="INV-202611-09999")
    (c,) = rank_candidates(
        transfer_amount_idr=5_000_000, transfer_date=DAY, payer_memo=GATEWAY, open_invoices=[inv]
    )
    assert c.name_score < NAME_HINT_FLOOR


def test_a_payer_with_only_a_first_name_does_not_reach_the_floor_on_a_first_name_client() -> None:
    line = "ATM Prima TRF FR MARIO 9999999999979803 BANK PERMATA TRANSFER RINTIS"
    assert extract_payer_name(line) == "MARIO"
    assert _name_score(line, "Mario") < NAME_HINT_FLOOR
