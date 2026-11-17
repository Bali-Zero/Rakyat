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


_HEADER = (
    "Post Date,Payment Date,Payment Type,Other Reference,Bank Reference,"
    "Debit Amount,Credit Amount,Balance,Description,Cheque Number"
)


def _mini(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Tiny statement text: rows are (post date, type, debit, credit, balance), as listed."""
    body = "\n".join(f"{ts},,{pt},R,B,{d},{c},{b},desc," for ts, pt, d, c, b in rows)
    return f"Account No.,1\nAccount Name,X\n\n{_HEADER}\n{body}\n"


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
    # adjacent rows keep their true order: transfer, then its commission
    assert [t.raw_row["Payment Type"] for t in txns[2:4]] == ["TR TO REMITT", "REMITTANCE COMMISSION"]


def test_cimb_same_second_rows_keep_true_order() -> None:
    # Same timestamp to the second: only the balance chain can tell the order.
    newest_first = _mini([
        ("04 Nov 2026 11:00:00", "REMITTANCE COMMISSION", "2500.00", "0.00", "9497500.00"),
        ("04 Nov 2026 11:00:00", "TR TO REMITT", "500000.00", "0.00", "9500000.00"),
    ])
    txns = parse_cimb_statement(newest_first).transactions
    assert [t.raw_row["Payment Type"] for t in txns] == ["TR TO REMITT", "REMITTANCE COMMISSION"]
    assert check_running_balance(txns) == []


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
    # an unquoted description containing a comma survives (the bank never quotes)
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
        # exact after trimming: the bank writes its types upper-case with single spaces
        ("bill payment", "empn billing 1", "other"),
        ("BILL PAYMENT", "Empn Billing 1", "other"),
        ("BILL PAYMENT", "  EMPN BILLING 1", "government_fee"),
        (" DIRECT DEBIT\t", "x", "payroll"),
        ("direct debit", "x", "other"),
        ("d\u0131rect deb\u0131t", "x", "other"),  # dotless i upper-cases to DIRECT DEBIT
        ("TR  TO REMITT", "x", "other"),
        ("TR\tTO REMITT", "x", "other"),
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


# ── correction round: orientation, row shape, strict amounts, kind vs direction ──


def _ts_rows_equal_stamp(order: str) -> str:
    older = ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "100.00", "0.00", "900.00")
    newer = ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "200.00", "0.00", "700.00")
    return _mini([newer, older] if order == "newest_first" else [older, newer])


def test_cimb_gate_reproducer_dates_beat_a_corrupted_balance() -> None:
    # S4.1: newest-first, distinct dates, the newer balance altered (700.00 -> 1000.00).
    text = _mini([
        ("02 Nov 2026 10:00:00", "DIRECT DEBIT", "200.00", "0.00", "1000.00"),
        ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "100.00", "0.00", "900.00"),
    ])
    stmt = parse_cimb_statement(text)
    report = build_preview_report(stmt)
    assert report["period"] == {"first": "2026-11-01T10:00:00", "last": "2026-11-02T10:00:00"}
    assert report["balance_check"] == {"ok": False, "mismatch_positions": [1]}
    assert report["order_decided_by"] == "timestamps"


def test_cimb_equal_second_newest_first_is_decided_by_balance_chain() -> None:
    # S4.2
    stmt = parse_cimb_statement(_ts_rows_equal_stamp("newest_first"))
    assert stmt.order_decided_by == "balance_chain"
    assert build_preview_report(stmt)["order_decided_by"] == "balance_chain"
    assert check_running_balance(stmt.transactions) == []


def test_cimb_equal_second_oldest_first_gives_the_same_result() -> None:
    # S4.3
    stmt = parse_cimb_statement(_ts_rows_equal_stamp("oldest_first"))
    assert stmt.order_decided_by == "balance_chain"
    assert stmt.opening_balance == Decimal("1000.00")
    assert check_running_balance(stmt.transactions) == []


def test_cimb_majority_of_timestamps_beats_a_few_ascending_pairs() -> None:
    # S4.4: the real shape. Newest-first, minutes apart, two pairs inverted by one second
    # (a batch stamped out of order); the chain is consistent in listing order.
    stamps = ["10:09:00", "10:08:00", "10:06:00", "10:06:01", "10:05:00",
              "10:04:00", "10:02:00", "10:02:01", "10:01:00", "10:00:00"]
    balance = 1000 - 10 * len(stamps)
    rows = []
    for hms in stamps:  # listing order is newest-first, so balances rise down the file
        rows.append((f"01 Nov 2026 {hms}", "DIRECT DEBIT", "10.00", "0.00", f"{balance}.00"))
        balance += 10
    stmt = parse_cimb_statement(_mini(rows))
    assert stmt.order_decided_by == "timestamps"
    assert stmt.transactions[0].posted_at.minute == 0  # reversed: last listed row first
    assert check_running_balance(stmt.transactions) == []


@pytest.mark.parametrize("inversion_seconds", [1, 2])
def test_cimb_pair_inverted_within_jitter_is_ordered_by_the_chain(inversion_seconds: int) -> None:
    # S4.6: two consistent rows listed newest-first, but the row listed first is stamped
    # EARLIER by 1-2 seconds (a batch). Dates that close carry no evidence: the chain decides.
    newer = ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "200.00", "0.00", "700.00")
    older = (f"01 Nov 2026 10:00:0{inversion_seconds}", "DIRECT DEBIT", "100.00", "0.00", "900.00")
    stmt = parse_cimb_statement(_mini([newer, older]))
    assert stmt.order_decided_by == "balance_chain"
    assert stmt.opening_balance == Decimal("1000.00")
    assert stmt.closing_balance == Decimal("700.00")
    assert check_running_balance(stmt.transactions) == []


def test_cimb_dates_three_seconds_apart_still_beat_a_corrupted_balance() -> None:
    # S4.7: just outside the jitter window the dates decide, and the alteration shows.
    text = _mini([
        ("01 Nov 2026 10:00:03", "DIRECT DEBIT", "200.00", "0.00", "1000.00"),  # true 700.00
        ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "100.00", "0.00", "900.00"),
    ])
    stmt = parse_cimb_statement(text)
    assert stmt.order_decided_by == "timestamps"
    assert [t.posted_at.second for t in stmt.transactions] == [0, 3]
    assert check_running_balance(stmt.transactions) == [1]


def test_cimb_single_row_is_default_order() -> None:
    # S4.5
    stmt = parse_cimb_statement(_mini([("01 Nov 2026 10:00:00", "DIRECT DEBIT", "5.00", "0.00", "95.00")]))
    assert stmt.order_decided_by == "default"
    assert len(stmt.transactions) == 1


@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
def test_cimb_equal_timestamps_orientation_decided_by_balance_chain(order: str) -> None:
    stmt = parse_cimb_statement(_ts_rows_equal_stamp(order))
    assert stmt.opening_balance == Decimal("1000.00")
    assert stmt.closing_balance == Decimal("700.00")
    assert check_running_balance(stmt.transactions) == []


@pytest.mark.parametrize("newest_first", [True, False])
def test_cimb_chain_tie_resolved_by_timestamps(newest_first: bool) -> None:
    older = ("01 Nov 2026 10:00:00", "REMITTANCE CR - BIFAST", "0.00", "100.00", "1100.00")
    newer = ("02 Nov 2026 10:00:00", "TR TO REMITT", "100.00", "0.00", "1000.00")
    text = _mini([newer, older] if newest_first else [older, newer])
    txns = parse_cimb_statement(text).transactions
    assert [t.posted_at.day for t in txns] == [1, 2]


def test_cimb_comma_in_description_keeps_description_cheque_and_amounts() -> None:
    row = "01 Nov 2026 10:00:00,,BILL PAYMENT,R,B,50.00,0.00,950.00,EMPN BILLING A, B, C,000777"
    txns = parse_cimb_statement(f"{_HEADER}\n{row}\n").transactions
    assert txns[0].description == "EMPN BILLING A, B, C"
    assert txns[0].raw_row["Cheque Number"] == "000777"
    assert txns[0].debit == Decimal("50.00")
    assert txns[0].balance == Decimal("950.00")
    assert txns[0].kind == "government_fee"


def test_cimb_row_without_optional_trailing_cells_is_accepted() -> None:
    row = "01 Nov 2026 10:00:00,,DIRECT DEBIT,R,B,50.00,0.00,950.00"
    txns = parse_cimb_statement(f"{_HEADER}\n{row}\n").transactions
    assert txns[0].raw_row["Description"] == ""
    assert txns[0].raw_row["Cheque Number"] == ""


def test_cimb_truncated_row_refused_with_line_number() -> None:
    text = _fixture_text().replace(
        "03 Nov 2026 10:15:00,03 Nov 2026,BILL PAYMENT,REF-002,BR-002,1000000.00,0.00,14000000.00,"
        "EMPN BILLING 0000000001,",
        "03 Nov 2026 10:15:00,03 Nov 2026,BILL PAYMENT,REF-002,BR-002,1000000.00",
    )
    with pytest.raises(BankParseError, match=r"file line 23"):
        parse_cimb_statement(text)


def test_cimb_single_truncated_row_refused() -> None:
    row = "01 Nov 2026 10:00:00,,DIRECT DEBIT,R,B"
    with pytest.raises(BankParseError, match="file line 2"):
        parse_cimb_statement(f"{_HEADER}\n{row}\n")


def _one_row(debit: str, credit: str, balance: str) -> str:
    return f"{_HEADER}\n01 Nov 2026 10:00:00,,DIRECT DEBIT,R,B,{debit},{credit},{balance},d,\n"


@pytest.mark.parametrize(
    ("debit", "credit", "balance"),
    [
        ("", "0.00", "1.00"),             # empty debit
        ("1.00", "", "1.00"),             # empty credit
        ("1.00", "0.00", ""),             # empty balance
        ("1e3", "0.00", "1.00"),          # exponent
        ("NaN", "0.00", "1.00"),
        ("1.00", "Infinity", "1.00"),
        ("0.00", "0.00", "inf"),
        ("-1.00", "0.00", "1.00"),        # sign on debit
        ("0.00", "+1.00", "1.00"),        # sign on credit
        ("1.000", "0.00", "1.00"),        # three decimals
        ("1.", "0.00", "1.00"),
        ("1234567890123456", "0.00", "1.00"),  # 16 digits
        ("1.00", "0.00", "1.5.0"),
        ("1.00", "0.00", "+1.00"),        # plus on balance
        ("1.00", "0.00", "\u0661.00"),    # non-ASCII digit
        ("007.00", "0.00", "1.00"),       # leading zeros
        ("1.00", "0.00", "00.50"),
    ],
)
def test_cimb_strict_amounts_refused_with_line(debit: str, credit: str, balance: str) -> None:
    with pytest.raises(BankParseError, match="file line 2"):
        parse_cimb_statement(_one_row(debit, credit, balance))


def test_cimb_negative_balance_allowed_and_plain_amounts_accepted() -> None:
    txns = parse_cimb_statement(_one_row("5", "0.00", "-1234567890123.5")).transactions
    assert txns[0].balance == Decimal("-1234567890123.5")


def test_cimb_both_sides_positive_refused() -> None:
    with pytest.raises(BankParseError, match="file line 2"):
        parse_cimb_statement(_one_row("1.00", "2.00", "1.00"))


def test_cimb_huge_exponent_refused_fast() -> None:
    import time

    start = time.perf_counter()
    with pytest.raises(BankParseError, match="file line 2"):
        parse_cimb_statement(_one_row("1e1000000", "0.00", "1.00"))
    assert time.perf_counter() - start < 1.0


def test_cimb_thousands_comma_in_an_amount_is_refused() -> None:
    # "1,000.00" in an unquoted cell splits into "1" and "000.00": refused, never read as 1 or 1000.
    row = "01 Nov 2026 10:00:00,,DIRECT DEBIT,R,B,1,000.00,0.00,1.00,d,"
    with pytest.raises(BankParseError, match="file line 2"):
        parse_cimb_statement(f"{_HEADER}\n{row}\n")


def test_cimb_quote_characters_are_plain_text_and_swallow_no_rows() -> None:
    # The bank never quotes fields: a stray quote must not merge the lines that follow it.
    lines = [
        "03 Nov 2026 10:00:00,,OVERBOOKING CR,R,B,0.00,100.00,1300.00,c,",
        '02 Nov 2026 10:00:00,,OVERBOOKING CR,R,B,0.00,100.00,1200.00,"TOKO unbalanced,',
        "01 Nov 2026 10:00:00,,OVERBOOKING CR,R,B,0.00,100.00,1100.00,a,",
    ]
    stmt = parse_cimb_statement(_HEADER + "\n" + "\n".join(lines) + "\n")
    assert len(stmt.transactions) == 3
    assert stmt.opening_balance == Decimal("1000.00")
    assert stmt.transactions[1].description == '"TOKO unbalanced'


def test_cimb_oversized_field_wrapped_in_parse_error() -> None:
    huge = "x" * 200_000
    with pytest.raises(BankParseError, match="unreadable"):
        parse_cimb_statement(f"{_HEADER}\n01 Nov 2026 10:00:00,,T,R,B,1.00,0.00,1.00,{huge},\n")


def test_cimb_kind_must_agree_with_direction() -> None:
    assert classify_cimb_kind("REMITTANCE CR - BIFAST", "x", "debit") == "other"
    assert classify_cimb_kind("TR TO REMITT", "x", "credit") == "other"
    assert classify_cimb_kind("CREDIT INTEREST", "x", "debit") == "other"
    assert classify_cimb_kind("BILL PAYMENT", "EMPN BILLING 1", "credit") == "other"
    assert classify_cimb_kind("WITHHOLDING TAX", "x", "credit") == "other"
    # unchanged normal mappings
    assert classify_cimb_kind("REMITTANCE CR - BIFAST", "x", "credit") == "client_credit"
    assert classify_cimb_kind("CREDIT INTEREST", "x", "credit") == "interest"
    assert classify_cimb_kind("TR TO REMITT", "x", "debit") == "transfer_out"


def test_cimb_kind_contradiction_through_the_parser() -> None:
    text = _mini([
        ("02 Nov 2026 10:00:00", "TR TO REMITT", "0.00", "50.00", "150.00"),
        ("01 Nov 2026 10:00:00", "REMITTANCE CR - BIFAST", "50.00", "0.00", "100.00"),
    ])
    assert [t.kind for t in parse_cimb_statement(text).transactions] == ["other", "other"]


def test_cimb_trailer_line_holding_a_single_space_is_ignored() -> None:
    text = _fixture_text() + "\n \n"
    assert len(parse_cimb_statement(text).transactions) == 13


def test_cimb_fixture_has_no_whitespace_only_line() -> None:
    assert all(line == "" or line.strip() for line in _fixture_text().split("\n"))


def test_cimb_legacy_integer_fields_round_half_up() -> None:
    text = _mini([("01 Nov 2026 10:00:00", "DIRECT DEBIT", "10.50", "0.00", "2.50")])
    t = parse_cimb_statement(text).transactions[0]
    assert t.amount_idr == 11  # not banker's rounding (10)
    assert t.running_balance_idr == 3  # not 2


# ── S7: no uploaded content in any error message ──

SENTINEL = "SENTINEL7f3a"
_VALID_ROW = [
    "01 Nov 2026 10:00:00", "01 Nov 2026", "DIRECT DEBIT", "REF", "BRF",
    "50.00", "0.00", "950.00", "desc", "123",
]
_ROW_COLUMNS = _HEADER.split(",")


@pytest.mark.parametrize("col", range(10))
def test_cimb_sentinel_in_any_column_never_reaches_the_message(col: int) -> None:
    cells = list(_VALID_ROW)
    cells[col] = SENTINEL
    text = f"Company Name,X\n\n{_HEADER}\n{','.join(cells)}\n"
    if _ROW_COLUMNS[col] in ("Post Date", "Debit Amount", "Credit Amount", "Balance"):
        with pytest.raises(BankParseError) as info:
            parse_cimb_statement(text)
        assert SENTINEL not in str(info.value)
        assert "file line 4" in str(info.value)
        assert _ROW_COLUMNS[col] in str(info.value)
    else:
        assert len(parse_cimb_statement(text).transactions) == 1


def test_cimb_sentinel_in_metadata_never_reaches_a_message() -> None:
    text = f"Company Name,{SENTINEL}\nAccount No.,{SENTINEL}\n\n{_HEADER}\n" + ",".join(
        [*_VALID_ROW[:5], "bad", "0.00", "950.00", "d", "1"]
    ) + "\n"
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(text)
    assert SENTINEL not in str(info.value)


def test_cimb_sentinel_in_trailer_never_reaches_the_message() -> None:
    row = ",".join(_VALID_ROW)
    shaped = ",".join([SENTINEL] * 10)
    text = f"{_HEADER}\n{row}\n\nGenerated By,{SENTINEL}\n{shaped}\n"
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(text)
    assert SENTINEL not in str(info.value)


def test_cimb_oversized_field_message_carries_no_csv_text() -> None:
    huge = SENTINEL + "x" * 200_000
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(f"{_HEADER}\n01 Nov 2026 10:00:00,,T,R,B,1.00,0.00,1.00,{huge},\n")
    assert SENTINEL not in str(info.value)
    assert "field larger" not in str(info.value)


def test_cimb_error_wording_is_fixed_text() -> None:
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(_one_row("1e3", "0.00", "1.00"))
    assert str(info.value) == "file line 2: Debit Amount is not a plain decimal amount"
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(f"{_HEADER}\nnot a date,,T,R,B,1.00,0.00,1.00,d,\n")
    assert str(info.value) == "file line 2: Post Date is not a timestamp"


def test_cimb_parser_logs_nothing_from_the_file(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        parse_cimb_statement(_fixture_text())
        with pytest.raises(BankParseError):
            parse_cimb_statement(_one_row(SENTINEL, "0.00", "1.00"))
    assert SENTINEL not in caplog.text
    assert "PT CONTOH UJI" not in caplog.text


# ── round 3: exact header, trim-only cells, strict Post Date, trailer shape, totality ──

_G_ROW = "01 Nov 2026 10:00:00,01 Nov 2026,DIRECT DEBIT,REF,BRF,5.00,0.00,95.00,invented,123"


def _g_text(row: str = _G_ROW, header: str = _HEADER) -> str:
    return f"{header}\n{row}\n"


def test_g1_lowercase_first_header_cell_is_no_header_found() -> None:
    with pytest.raises(BankParseError, match="no 'Post Date' header row found"):
        parse_cimb_statement(_g_text(header=_HEADER.replace("Post Date", "post date", 1)))


@pytest.mark.parametrize(
    "header",
    [
        _HEADER.replace("Debit Amount", "debit amount"),  # one column case-folded
        _HEADER + ",Extra",  # an eleventh column
        _HEADER.replace(",Cheque Number", ""),  # a column missing
        _HEADER.replace("Debit Amount,Credit Amount", "Credit Amount,Debit Amount"),  # reordered
        _HEADER.replace("Payment Date", "Debit Amount"),  # a duplicated name
    ],
)
def test_header_must_be_the_known_layout_exactly(header: str) -> None:
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(_g_text(header=header))
    assert str(info.value) == "statement header is not the CIMB Account Statement Summary layout"


def test_data_shaped_row_before_the_header_refuses_the_file() -> None:
    # Rows before the header are metadata (two cells in the export). A row long enough to
    # reach the Balance column there is a data row that would vanish silently.
    text = f"Account No.,7\n{_G_ROW}\n{_HEADER}\n{_G_ROW}\n"
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(text)
    assert str(info.value) == "file line 2: data row before the header"


def test_short_rows_before_the_header_are_metadata() -> None:
    text = f"Account No.,7\nAccount Name,X,with,commas\n\n{_HEADER}\n{_G_ROW}\n"
    stmt = parse_cimb_statement(text)
    assert stmt.account_number == "7"
    assert len(stmt.transactions) == 1


def test_line_ceiling_counts_an_unterminated_last_line() -> None:
    with pytest.raises(BankParseError, match="statement has too many lines"):
        parse_cimb_statement("x\n" * 20_000 + "x")  # 20,001 lines, no final newline
    with pytest.raises(BankParseError, match="no 'Post Date' header row found"):
        parse_cimb_statement("x\n" * 19_999 + "x")  # exactly 20,000 lines


def test_unreadable_text_reports_the_earliest_offending_line() -> None:
    text = "Account\rNo.,1\n\n\n\nA\x00B\n" + f"{_HEADER}\n{_G_ROW}\n"
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(text)
    assert str(info.value).startswith("file line 1: unreadable as CSV")


def test_metadata_value_keeps_its_commas() -> None:
    text = f"Account No.,7\nAccount Name,PT CONTOH, Tbk\n\n{_HEADER}\n{_G_ROW}\n"
    stmt = parse_cimb_statement(text)
    assert stmt.account_name == "PT CONTOH, Tbk"
    assert stmt.account_number == "7"


def test_preview_equals_parse_then_report() -> None:
    import backend.services.accounting.bank_parser as bp

    text = _fixture_text()
    assert bp.preview_cimb_statement(text) == build_preview_report(parse_cimb_statement(text))


def test_preview_is_total_like_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.services.accounting.bank_parser as bp

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"leak {SENTINEL}")

    monkeypatch.setattr(bp, "_money", boom)  # the failure is in the report, after the parse
    with pytest.raises(BankParseError) as info:
        bp.preview_cimb_statement(_fixture_text())
    assert str(info.value) == "statement could not be parsed"
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_header_cells_are_trimmed_not_case_folded() -> None:
    spaced = ",".join(f" {c} " for c in _HEADER.split(","))
    assert len(parse_cimb_statement(_g_text(header=spaced)).transactions) == 1


def test_g2_leading_apostrophe_on_an_amount_is_refused() -> None:
    with pytest.raises(BankParseError, match="file line 2: Debit Amount is not a plain decimal"):
        parse_cimb_statement(_g_text(_G_ROW.replace(",5.00,", ",'5.00,")))


def test_apostrophe_leading_description_and_reference_kept_verbatim() -> None:
    row = _G_ROW.replace(",REF,", ",'REF,").replace(",invented,", ",'invented,")
    t = parse_cimb_statement(_g_text(row)).transactions[0]
    assert t.description == "'invented"
    assert t.raw_row["Other Reference"] == "'REF"


def test_apostrophe_leading_post_date_is_refused() -> None:
    with pytest.raises(BankParseError, match="file line 2: Post Date is not a timestamp"):
        parse_cimb_statement(_g_text("'" + _G_ROW))


def test_g3_overflowing_post_date_is_a_parse_error() -> None:
    row = _G_ROW.replace("01 Nov 2026 10:00:00", "01 Nov 99999999999999999999 10:00:00", 1)
    with pytest.raises(BankParseError, match="file line 2: Post Date is not a timestamp"):
        parse_cimb_statement(_g_text(row))


@pytest.mark.parametrize(
    "stamp",
    [
        "30 Feb 2026 10:00:00",   # not a calendar date
        "01 Nov 2026 25:00:00",   # not a time
        "01 Nov 2026 10:60:00",
        "01 Nov 2026",            # no time of day
        "1 Nov 2026 10:00:00",    # one-digit day
        "01 nov 2026 10:00:00",   # lowercase month
        "01 Xyz 2026 10:00:00",   # not an English month
        "01 Nov 2026  10:00:00",  # double space
        "01 Nov 0000 10:00:00",   # year 0
        "01 Nov 2026 10:00:00.5",
        "01 Nov 2026 \uff11\uff10:00:00",  # full-width digits
    ],
)
def test_post_date_strict_pattern_refused_with_line(stamp: str) -> None:
    row = _G_ROW.replace("01 Nov 2026 10:00:00", stamp, 1)
    with pytest.raises(BankParseError, match="file line 2: Post Date is not a timestamp"):
        parse_cimb_statement(_g_text(row))


def test_post_date_surrounding_whitespace_is_trimmed() -> None:
    row = _G_ROW.replace("01 Nov 2026 10:00:00", "  01 Nov 2026 10:00:00 ", 1)
    assert parse_cimb_statement(_g_text(row)).transactions[0].posted_at.hour == 10


def test_data_shaped_row_after_blank_line_refused_whatever_its_first_cell() -> None:
    text = _g_text() + "\nGenerated By,a,b,c,d,e,f,g,h,i\n"
    with pytest.raises(BankParseError, match="file line 4: data row after a blank line"):
        parse_cimb_statement(text)


def test_row_after_blank_line_reaching_balance_column_refused() -> None:
    text = _g_text() + "\n" + ",".join(["x"] * 8) + "\n"
    with pytest.raises(BankParseError, match="after a blank line"):
        parse_cimb_statement(text)


def test_short_rows_after_blank_line_are_trailer() -> None:
    text = _g_text() + "\nGenerated By,someone\n" + ",".join(["x"] * 7) + "\n \n"
    assert len(parse_cimb_statement(text).transactions) == 1


def test_totality_unexpected_error_becomes_fixed_parse_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import backend.services.accounting.bank_parser as bp

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"leak {SENTINEL}")

    monkeypatch.setattr(bp, "_orient_oldest_first", boom)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BankParseError) as info:
            parse_cimb_statement(_fixture_text())
    err = info.value
    assert str(err) == "statement could not be parsed"
    assert err.__cause__ is None
    assert err.__context__ is None
    assert SENTINEL not in str(err)
    records = [r for r in caplog.records if r.name == bp.logger.name]
    assert len(records) == 1
    assert "RuntimeError" in records[0].getMessage()
    assert SENTINEL not in caplog.text
    assert records[0].exc_info is None


def test_a_parse_error_passes_through_the_guard_unlogged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.DEBUG), pytest.raises(BankParseError, match="no 'Post Date'"):
        parse_cimb_statement("a,b\n")
    assert caplog.records == []


def test_mutation_fuzz_only_statement_or_parse_error() -> None:
    import random
    import re

    rng = random.Random(20261117)
    amount = re.compile(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?")
    junk = ["\x00", "\u00e9", "\u0661", "'", '"', "-", "+", "e", "9", ",", " ", "\t", "\ufeff", "\r"]
    cell_fills = ["9" * 3000, "1e1000000", "-5", "+5", '"', "'", "NaN", "Infinity", "", "0" * 40]
    base = _fixture_text()
    outcomes = {"ok": 0, "refused": 0}
    for _ in range(400):
        text = base
        for _step in range(rng.randint(1, 4)):
            op = rng.choice(["char", "cell", "dup", "drop", "trunc"])
            lines = text.split("\n")
            if op == "char" and text:
                i = rng.randrange(len(text))
                text = text[:i] + rng.choice(junk) + text[i + 1 :]
            elif op == "cell":
                n = rng.randrange(len(lines))
                cells = lines[n].split(",")
                cells[rng.randrange(len(cells))] = rng.choice(cell_fills)
                lines[n] = ",".join(cells)
                text = "\n".join(lines)
            elif op == "dup":
                n = rng.randrange(len(lines))
                lines.insert(n, lines[n])
                text = "\n".join(lines)
            elif op == "drop":
                del lines[rng.randrange(len(lines))]
                text = "\n".join(lines)
            else:
                text = text[: rng.randrange(len(text) + 1)]
        try:
            stmt = parse_cimb_statement(text)
        except BankParseError:
            outcomes["refused"] += 1
            continue
        outcomes["ok"] += 1
        for t in stmt.transactions:
            for col in ("Debit Amount", "Credit Amount", "Balance"):
                assert amount.fullmatch(t.raw_row[col]), col
    assert outcomes["ok"] > 0 and outcomes["refused"] > 0


# ── round 4: spec audit (S1 trim, blank rows, layout, BOM, csv errors, ceilings, kind, period) ──


@pytest.mark.parametrize("ws", ["\u00a0", "\u0085", "\x1c", "\u2003", "\x0b"])
@pytest.mark.parametrize("column", ["amount", "post_date"])
def test_trim_is_spaces_and_tabs_only_for_amounts_and_post_date(ws: str, column: str) -> None:
    if column == "amount":
        row = _G_ROW.replace(",5.00,", f",{ws}5.00{ws},")
        message = "file line 2: Debit Amount is not a plain decimal amount"
    else:
        row = _G_ROW.replace("01 Nov 2026 10:00:00", f"{ws}01 Nov 2026 10:00:00{ws}", 1)
        message = "file line 2: Post Date is not a timestamp"
    with pytest.raises(BankParseError, match=message):
        parse_cimb_statement(_g_text(row))


def test_spaces_and_tabs_are_trimmed_everywhere() -> None:
    row = _G_ROW.replace(",5.00,", ",\t 5.00 \t,").replace("invented", " \tinvented\t ")
    t = parse_cimb_statement(_g_text(row)).transactions[0]
    assert t.debit == Decimal("5.00")
    assert t.description == "invented"


def test_description_keeps_a_leading_non_breaking_space() -> None:
    t = parse_cimb_statement(_g_text(_G_ROW.replace("invented", "\u00a0invented"))).transactions[0]
    assert t.description == "\u00a0invented"


@pytest.mark.parametrize("blank", [",,,,,,,,,", " , \t,,,,,,,,", ",,,,,,,,,,,,"])
def test_blank_row_before_the_header_is_blank_not_data(blank: str) -> None:
    assert len(parse_cimb_statement(f"{blank}\n{_g_text()}").transactions) == 1


@pytest.mark.parametrize("blank", [",,,,,,,,,", " , \t,,,,,,,,", ",,,,,,,,,,,,"])
def test_blank_row_in_the_trailer_is_blank_not_data(blank: str) -> None:
    assert len(parse_cimb_statement(f"{_g_text()}\n{blank}\nGenerated By,x\n").transactions) == 1


def test_header_blank_row_trailer_is_refused_as_no_rows() -> None:
    with pytest.raises(BankParseError, match="statement header found but no transaction rows"):
        parse_cimb_statement(f"{_HEADER}\n\nGenerated By,x\nGenerated On,y\n")


def test_table_ends_at_the_first_blank_row_even_before_any_data() -> None:
    with pytest.raises(BankParseError, match="no transaction rows"):
        parse_cimb_statement(f"{_HEADER}\n,,,,,,,,,\n{_G_ROW}\n")


def test_header_must_be_within_the_first_64_lines() -> None:
    meta = "Key,Value\n"
    assert len(parse_cimb_statement(meta * 63 + _g_text()).transactions) == 1  # header on line 64
    with pytest.raises(BankParseError, match="no 'Post Date' header row found"):
        parse_cimb_statement(meta * 64 + _g_text())  # header on line 65


def test_only_one_bom_is_tolerated() -> None:
    assert len(parse_cimb_statement("\ufeff" + _g_text()).transactions) == 1
    with pytest.raises(BankParseError, match="file line 1: more than one byte order mark"):
        parse_cimb_statement("\ufeff\ufeff" + _g_text())
    with pytest.raises(BankParseError, match="more than one byte order mark"):
        parse_cimb_statement("\ufeff\ufeff" + _fixture_text())  # metadata first, not the header


@pytest.mark.parametrize(
    "bad",
    [
        _G_ROW.replace("REF", "R\rX"),                       # lone CR inside a cell
        _G_ROW.replace("REF", "R\x00X"),                     # NUL
        _G_ROW.replace("invented", "x" * 200_000),           # beyond the csv field limit
    ],
    ids=["lone_cr", "nul", "oversized_cell"],
)
def test_csv_level_errors_carry_the_line_and_no_content(bad: str) -> None:
    bad = bad.replace("R\rX", f"R\r{SENTINEL}").replace("R\x00X", f"R\x00{SENTINEL}")
    with pytest.raises(BankParseError) as info:
        parse_cimb_statement(f"{_HEADER}\n{bad}\n")
    msg = str(info.value)
    assert msg.startswith("file line 2: unreadable as CSV")
    assert SENTINEL not in msg


def test_crlf_line_endings_are_accepted() -> None:
    assert len(parse_cimb_statement(_fixture_text().replace("\n", "\r\n")).transactions) == 13


def test_more_than_20000_lines_refused_before_parsing() -> None:
    import time

    start = time.perf_counter()
    with pytest.raises(BankParseError, match="statement has too many lines"):
        parse_cimb_statement("\n" * 20_001)
    assert time.perf_counter() - start < 0.5
    # exactly at the ceiling it is the layout that is refused, not the size
    with pytest.raises(BankParseError, match="no 'Post Date' header row found"):
        parse_cimb_statement("\n" * 20_000)


@pytest.mark.parametrize(
    ("payment_type", "expected"),
    [
        ("REMITTANCE CRAZY", "other"),
        ("OVERBOOKING CRX", "other"),
        ("OVERBOOKING CR", "client_credit"),
        ("OVERBOOKING FROM CA", "client_credit"),
        ("OVERBOOKING FROM CAX", "other"),
        ("REMITTANCE CR - RTGS", "client_credit"),
        ("REMITTANCE CR -", "other"),
        ("  REMITTANCE CR - SKN ", "client_credit"),  # trimmed, nothing else
        ("  remittance   cr  -  skn ", "other"),  # no case folding, no space collapsing
    ],
)
def test_client_credit_needs_the_exact_bank_wording(payment_type: str, expected: str) -> None:
    text = _mini([("01 Nov 2026 10:00:00", payment_type, "0.00", "50.00", "150.00")])
    assert parse_cimb_statement(text).transactions[0].kind == expected


def test_a_zero_zero_row_is_other_with_debit_direction() -> None:
    row = "01 Nov 2026 10:00:00,,BILL PAYMENT,R,B,0.00,0.00,95.00,EMPN BILLING 1,"
    t = parse_cimb_statement(f"{_HEADER}\n{row}\n").transactions[0]
    assert t.kind == "other"
    assert t.direction == "debit"
    assert t.debit == t.credit == Decimal(0)


def test_report_period_is_earliest_and_latest_whatever_the_row_order() -> None:
    # listed newest-first, but the first listed row is one second EARLIER than the last
    text = _mini([
        ("01 Nov 2026 10:00:00", "DIRECT DEBIT", "100.00", "0.00", "800.00"),
        ("01 Nov 2026 10:00:01", "DIRECT DEBIT", "100.00", "0.00", "900.00"),
    ])
    stmt = parse_cimb_statement(text)
    assert stmt.transactions[0].posted_at > stmt.transactions[-1].posted_at  # chain order
    period = build_preview_report(stmt)["period"]
    assert period == {"first": "2026-11-01T10:00:00", "last": "2026-11-01T10:00:01"}


def test_rejoined_description_is_joined_as_split_then_trimmed_as_one_cell() -> None:
    row = "01 Nov 2026 10:00:00,,DIRECT DEBIT,R,B,5.00,0.00,95.00,  A , B,C  ,123"
    t = parse_cimb_statement(f"{_HEADER}\n{row}\n").transactions[0]
    assert t.description == "A , B,C"
    assert t.raw_row["Cheque Number"] == "123"
