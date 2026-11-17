"""Tests for the bank statement CSV parser (Indonesian gotchas + balance check)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from backend.services.accounting.bank_parser import (
    _CONFIGS,
    BankConfig,
    BankParseError,
    build_preview_report,
    check_running_balance,
    classify_cimb_kind,
    parse_cimb_statement,
    parse_csv,
    verify_balance,
)


def test_leading_apostrophe_and_skip_blank_date() -> None:
    csv_text = (
        "date,description,amount,balance\n"
        "'2026-06-25,GUARDED DATE,1000001,1000001\n"
        ",TOTAL ROW (no date),,\n"
    )
    txns = parse_csv(csv_text, bank_code="generic")
    assert len(txns) == 1
    assert txns[0].txn_date == date(2026, 6, 25)


def test_generic_signed_amount() -> None:
    csv_text = (
        "date,description,amount,balance\n"
        "2026-06-25,incoming,4000000,84000510\n"
        "2026-06-26,outgoing,-500000,9500000\n"
    )
    txns = parse_csv(csv_text, bank_code="generic")
    assert txns[0].direction == "credit" and txns[0].amount_idr == 4_000_000
    assert txns[1].direction == "debit" and txns[1].amount_idr == 500_000


def test_flag_mode() -> None:
    cfg = BankConfig(
        bank_code="flagbank", date_col="d", desc_col="m",
        amount_col="amt", flag_col="dc", decimal_comma=False,
    )
    _CONFIGS["flagbank"] = cfg
    csv_text = "d,m,amt,dc\n25/06/2026,in,4000000,CR\n26/06/2026,out,500000,DB\n"
    txns = parse_csv(csv_text, bank_code="flagbank")
    assert txns[0].direction == "credit"
    assert txns[1].direction == "debit"


def test_unknown_bank_raises() -> None:
    with pytest.raises(BankParseError):
        parse_csv("a,b\n1,2\n", bank_code="nope")


def test_wrong_delimiter_raises_not_silent_empty() -> None:
    # D7 (superscar #2 — no green-but-empty): a real CIMB export is semicolon-
    # delimited, but a comma-delimited config would read the whole line as one
    # column -> no date_col -> every data row skipped -> [] silently WITHOUT
    # the guard. That must fail loudly instead.
    cfg = BankConfig(
        bank_code="cimb_wrongdelim",
        date_col="Tanggal", desc_col="Keterangan",
        debit_col="Debet", credit_col="Kredit", balance_col="Saldo",
        decimal_comma=True, delimiter=",",  # WRONG: should be ';'
    )
    _CONFIGS["cimb_wrongdelim"] = cfg
    semicolon_content = (
        "Tanggal;Keterangan;Debet;Kredit;Saldo\n"
        "25/06/2026;TRANSFER DARI PUGU;;4.000.000,00;84.000.510,00\n"
        "26/06/2026;BIAYA ADMIN;15.000,00;;9.985.000,00\n"
    )
    with pytest.raises(BankParseError, match="parsed 0 transactions"):
        parse_csv(semicolon_content, bank_code="cimb_wrongdelim")


def test_header_only_no_data_rows_returns_empty_not_raise() -> None:
    # Innocence case for D7: a header with NO data rows is not a wrong-delimiter
    # failure (skipped == 0), so it must return [] quietly, never raise.
    out = parse_csv("date,description,amount,balance\n", bank_code="generic")
    assert out == []


def test_verify_balance_ok_and_fail() -> None:
    csv_text = (
        "date,description,amount,balance\n"
        "2026-06-25,IN,4000000,14000000\n"
        "2026-06-26,OUT,-1000000,13000000\n"
    )
    txns = parse_csv(csv_text, bank_code="generic")
    # opening 10M + 4M - 1M = 13M closing
    assert verify_balance(txns, 10_000_000, 13_000_000) is True
    assert verify_balance(txns, 10_000_000, 99_999_999) is False


# ── CIMB Niaga OCTO Biz "Account Statement Summary" (real layout, synthetic data) ──

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cimb_octo_statement_synthetic.csv"


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_cimb_preamble_skipped_and_metadata_captured() -> None:
    stmt = parse_cimb_statement(_fixture_text())
    assert stmt.account_number == "700000000001"
    assert stmt.account_name == "PT CONTOH UJI"
    assert len(stmt.transactions) == 13


def test_cimb_header_located_by_first_cell_not_line_number() -> None:
    # Two extra preamble lines shift the header; it must still be found.
    shifted = "Extra Key,Extra Value\nAnother,Line\n" + _fixture_text()
    assert len(parse_cimb_statement(shifted).transactions) == 13


def test_cimb_missing_header_raises() -> None:
    with pytest.raises(BankParseError, match="Post Date"):
        parse_cimb_statement("Company ID,X\nAccount No.,1\n")


def test_cimb_empty_content_raises() -> None:
    with pytest.raises(BankParseError):
        parse_cimb_statement("")


def test_cimb_header_without_rows_raises() -> None:
    header = _fixture_text().split("\n\n", 1)[1].splitlines()[0]
    with pytest.raises(BankParseError, match="no transaction rows"):
        parse_cimb_statement(header + "\n")


def test_cimb_utf8_bom_tolerated() -> None:
    stmt = parse_cimb_statement("\ufeff" + _fixture_text())
    assert stmt.account_number == "700000000001"
    assert len(stmt.transactions) == 13


def test_cimb_timestamp_keeps_time_of_day() -> None:
    txns = parse_cimb_statement(_fixture_text()).transactions
    assert txns[0].posted_at == datetime(2026, 11, 2, 9, 0, 0)
    assert txns[0].txn_date == date(2026, 11, 2)
    assert txns[-1].posted_at == datetime(2026, 11, 13, 16, 0, 0)


def test_cimb_newest_first_input_returned_oldest_first() -> None:
    txns = parse_cimb_statement(_fixture_text()).transactions
    stamps = [t.posted_at for t in txns]
    assert stamps == sorted(stamps)
    # same-second-adjacent rows keep their true order: transfer, then its commission
    assert [t.raw_row["Payment Type"] for t in txns[2:4]] == ["TR TO REMITT", "REMITTANCE COMMISSION"]


def test_cimb_oldest_first_input_is_not_reversed() -> None:
    lines = _fixture_text().splitlines()
    head, rows = lines[:11], lines[11:24]
    flipped = "\n".join(head + rows[::-1]) + "\n"
    txns = parse_cimb_statement(flipped).transactions
    assert txns[0].posted_at == datetime(2026, 11, 2, 9, 0, 0)


def test_cimb_row_fields_carried() -> None:
    txns = parse_cimb_statement(_fixture_text()).transactions
    cash = next(t for t in txns if t.raw_row["Payment Type"] == "CHQ CASH WITHDRAWAL")
    assert cash.raw_row["Cheque Number"] == "000123"
    assert cash.raw_row["Bank Reference"] == "BR-005"
    assert cash.raw_row["Other Reference"] == "REF-005"
    assert cash.direction == "debit"
    assert cash.debit == Decimal("3000000.00")
    assert cash.amount_idr == 3_000_000
    # a quoted description containing a comma survives
    assert any(t.description == "Invoice 11, sample client" for t in txns)


def test_cimb_opening_and_closing_balance_derived_from_rows() -> None:
    stmt = parse_cimb_statement(_fixture_text())
    assert stmt.opening_balance == Decimal("10000000.00")
    assert stmt.closing_balance == Decimal("9217400.00")


def test_cimb_running_balance_clean_file_has_no_mismatch() -> None:
    assert check_running_balance(parse_cimb_statement(_fixture_text()).transactions) == []


def test_cimb_tampered_balance_reported_with_position() -> None:
    # Tamper the balance of the 5th oldest row (CHQ CASH WITHDRAWAL, position 4).
    tampered = _fixture_text().replace("8997500.00", "8997501.00")
    txns = parse_cimb_statement(tampered).transactions
    # the tampered row itself and the next row (whose predecessor balance changed)
    assert check_running_balance(txns) == [4, 5]


def test_cimb_tampered_amount_reported_at_that_row_only() -> None:
    tampered = _fixture_text().replace("6000.00,0.00,8981500.00", "6001.00,0.00,8981500.00")
    txns = parse_cimb_statement(tampered).transactions
    assert check_running_balance(txns) == [6]


def test_cimb_kind_mapping_per_row() -> None:
    txns = parse_cimb_statement(_fixture_text()).transactions
    assert [t.kind for t in txns] == [
        "client_credit",   # REMITTANCE CR - BIFAST
        "government_fee",  # BILL PAYMENT + EMPN BILLING
        "transfer_out",    # TR TO REMITT
        "transfer_fee",    # REMITTANCE COMMISSION
        "cash_withdrawal", # CHQ CASH WITHDRAWAL
        "bank_fee",        # CHEQUE STAMP DUTY
        "bank_fee",        # MISCELLANEOUS CHARGES
        "payroll",         # DIRECT DEBIT
        "tax",             # WITHHOLDING TAX
        "interest",        # CREDIT INTEREST
        "client_credit",   # OVERBOOKING CR
        "bank_fee",        # MONTHLY ADMIN FEE
        "other",           # unknown type never raises
    ]


@pytest.mark.parametrize(
    ("payment_type", "description", "expected"),
    [
        ("REMITTANCE CR - SKN", "x", "client_credit"),
        ("REMITTANCE CR - SWIFT", "x", "client_credit"),
        ("OVERBOOKING FROM CA", "x", "client_credit"),
        ("BILL PAYMENT", "EMPN BILLING 1", "government_fee"),
        ("BILL PAYMENT", "SOMETHING ELSE", "other"),
        ("bill payment", "empn billing 1", "government_fee"),
        ("", "", "other"),
    ],
)
def test_cimb_kind_edge_cases(payment_type: str, description: str, expected: str) -> None:
    assert classify_cimb_kind(payment_type, description) == expected


def test_cimb_trailer_after_blank_line_ignored() -> None:
    # the fixture ends with a blank line + "Generated By/On" trailer
    assert _fixture_text().rstrip().splitlines()[-1].startswith("Generated On")
    assert len(parse_cimb_statement(_fixture_text()).transactions) == 13


def test_cimb_data_row_after_blank_line_refused() -> None:
    lines = _fixture_text().splitlines()
    cut = lines.index("") + 1 + 1 + 5  # blank, header, five data rows
    broken = "\n".join(lines[:cut] + [""] + lines[cut:]) + "\n"
    with pytest.raises(BankParseError, match="after a blank line"):
        parse_cimb_statement(broken)


def test_cimb_unparseable_row_raises_loudly() -> None:
    header = _fixture_text().split("\n\n", 1)[1].splitlines()[0]
    with pytest.raises(BankParseError, match="file line"):
        parse_cimb_statement(header + "\nnot a date,,X,,,1.00,0.00,1.00,d,\n")


def test_cimb_preview_report_shape() -> None:
    report = build_preview_report(parse_cimb_statement(_fixture_text()))
    assert report["account"] == {"number": "700000000001", "name": "PT CONTOH UJI"}
    assert report["period"] == {"first": "2026-11-02T09:00:00", "last": "2026-11-13T16:00:00"}
    assert report["row_count"] == 13
    assert report["opening_balance"] == "10000000.00"
    assert report["closing_balance"] == "9217400.00"
    assert report["total_credit"] == "5752000.00"
    assert report["total_debit"] == "6534600.00"
    assert report["balance_check"] == {"ok": True, "mismatch_positions": []}
    assert report["kinds"]["bank_fee"] == {"count": 3, "total": "31000.00"}
    assert report["kinds"]["client_credit"] == {"count": 2, "total": "5750000.00"}
    assert report["kinds"]["other"] == {"count": 1, "total": "100.00"}
