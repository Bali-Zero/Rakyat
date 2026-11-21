"""Mandiri and Permata statement readers, and the one upload entry point (invented data only)."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal

import pytest

from backend.services.accounting.bank_parser import BankParseError, check_running_balance
from backend.services.accounting.bank_pdf_readers import (
    Word,
    detect_pdf_format,
    extract_pdf_pages,
    parse_and_preview_statement,
    parse_bni_pages,
    parse_mandiri_pages,
    parse_permata_pages,
    preview_statement,
)
from backend.tests.fixtures.bank_pdf_builder import (
    ACCOUNT,
    NAME,
    bni_page,
    mandiri_page,
    permata_page,
    words_to_pdf,
)
from backend.tests.fixtures.bank_statement_builder import at, statement_text

# ── Mandiri ─────────────────────────────────────────────────────────────────

M_ROWS = [  # oldest first: opening 10,000,000.00 -> 2,500,000.00 debit, 1,000,000.00 credit, 25,000.00 fee
    (
        "16 Nov 2026",
        "09:15:00",
        ["Transfer keluar", "ref satu"],
        "2,500,000.00",
        "0.00",
        "7,500,000.00",
    ),
    ("16 Nov 2026", "10:30:05", ["Setoran masuk"], "0.00", "1,000,000.00", "8,500,000.00"),
    ("17 Nov 2026", "23:59:00", ["Biaya Adm 17506"], "25,000.00", "0.00", "8,475,000.00"),
]


def _mandiri(rows=M_ROWS, **kw):
    args = {
        "opening": "10,000,000.00",
        "closing": "8,475,000.00",
        "total_debit": "2,525,000.00",
        "total_credit": "1,000,000.00",
    } | kw
    return mandiri_page(rows, **args)


def test_mandiri_reads_every_row_exactly_oldest_first() -> None:
    stmt = parse_mandiri_pages([_mandiri()])
    assert (stmt.bank_code, stmt.source_format) == ("bank_mandiri", "pdf")
    assert (stmt.account_number, stmt.account_name) == (ACCOUNT, NAME)
    t = stmt.transactions
    assert [x.posted_at for x in t] == [
        datetime(2026, 11, 16, 9, 15),
        datetime(2026, 11, 16, 10, 30, 5),
        datetime(2026, 11, 17, 23, 59),
    ]
    assert [(x.debit, x.credit, x.balance) for x in t][0] == (
        Decimal("2500000.00"),
        Decimal(0),
        Decimal("7500000.00"),
    )
    assert t[0].description == "Transfer keluar ref satu"
    assert stmt.opening_balance == Decimal("10000000.00") and stmt.closing_balance == Decimal(
        "8475000.00"
    )
    assert check_running_balance(t) == []
    assert [x.kind for x in t] == ["other", "other", "bank_fee"]


def test_mandiri_a_file_listed_newest_first_is_turned_around() -> None:
    stmt = parse_mandiri_pages([_mandiri(M_ROWS[::-1])])
    assert [x.balance for x in stmt.transactions] == [
        Decimal("7500000.00"),
        Decimal("8500000.00"),
        Decimal("8475000.00"),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"currency": "USD"},
        {"total_debit": "2,525,001.00"},
        {"total_credit": "1,000,001.00"},
        {"closing": "8,475,001.00"},
        {"opening": "10,000,001.00"},
    ],
    ids=["not_idr", "debit_total", "credit_total", "closing", "opening"],
)
def test_mandiri_refuses_what_does_not_add_up(kwargs) -> None:
    with pytest.raises(BankParseError):
        parse_mandiri_pages([_mandiri(**kwargs)])


def test_mandiri_a_broken_running_balance_is_reported_not_hidden() -> None:
    rows = [M_ROWS[0], (*M_ROWS[1][:5], "8,600,000.00"), M_ROWS[2]]
    stmt = parse_mandiri_pages([_mandiri(rows)])
    assert check_running_balance(stmt.transactions) == [1, 2]


def test_mandiri_two_accounts_in_one_file_are_refused() -> None:
    with pytest.raises(BankParseError, match="more than one account"):
        parse_mandiri_pages([_mandiri(), _mandiri(account="0000000002")])


def test_mandiri_the_same_account_on_two_pages_is_one_statement() -> None:
    first = _mandiri(
        M_ROWS[:1], closing="7,500,000.00", total_debit="2,500,000.00", total_credit="0.00"
    )
    second = _mandiri(M_ROWS[1:])
    # a continuation page repeats the header: it only reads as one statement when it is identical
    with pytest.raises(BankParseError):
        parse_mandiri_pages([first, second])
    both = [_mandiri(), _mandiri()]
    with pytest.raises(BankParseError):  # the same rows twice do not add up to the printed totals
        parse_mandiri_pages(both)


def test_mandiri_a_row_with_two_amounts_on_one_side_is_refused() -> None:
    rows = [("16 Nov 2026", "09:15:00", ["x"], "5.00", "5.00", "10,000,000.00")]
    with pytest.raises(BankParseError):
        parse_mandiri_pages(
            [_mandiri(rows, closing="10,000,000.00", total_debit="5.00", total_credit="5.00")]
        )


def test_mandiri_a_long_remark_stays_whole_next_to_a_short_one() -> None:
    rows = [
        ("16 Nov 2026", "14:11:00", ["ALPHA", "A1", "A2", "A3", "A4"], "100.00", "0.00", "900.00"),
        ("16 Nov 2026", "16:02:00", ["BETA"], "0.00", "200.00", "1,100.00"),
        ("17 Nov 2026", "09:00:00", ["GAMMA", "G1"], "0.00", "50.00", "1,150.00"),
    ]
    page = mandiri_page(
        rows, opening="1,000.00", closing="1,150.00", total_debit="100.00", total_credit="250.00"
    )
    stmt = parse_mandiri_pages([page])
    assert [t.description for t in stmt.transactions] == ["ALPHA A1 A2 A3 A4", "BETA", "GAMMA G1"]
    assert [(t.debit, t.credit) for t in stmt.transactions][0] == (Decimal(100), Decimal(0))


def test_mandiri_rows_drawn_with_no_gap_are_refused_not_guessed() -> None:
    page = mandiri_page(
        M_ROWS,
        opening="10,000,000.00",
        closing="8,475,000.00",
        total_debit="2,525,000.00",
        total_credit="1,000,000.00",
        gap=3.0,
    )
    with pytest.raises(BankParseError, match="row block"):
        parse_mandiri_pages([page])


def test_mandiri_a_block_without_an_amount_line_or_with_stray_text_is_refused() -> None:
    page = _mandiri()
    stray = [*page, Word("STRAY", 350, 500, 520)]  # text in the amount area, outside any amount
    with pytest.raises(BankParseError, match="row block"):
        parse_mandiri_pages([stray])
    lone_text = [*page, Word("lost", 120, 560, 530)]  # a remark with no date, time or amounts
    with pytest.raises(BankParseError, match="row block"):
        parse_mandiri_pages([lone_text])


def test_mandiri_another_account_block_inside_the_table_is_refused() -> None:
    # the header prints labels on one line and values on the next; a merged export's block
    # inside the table is a block that is not a transaction row, which the body parse refuses
    other = [
        Word("Account", 31, 60, 600),
        Word("No.", 62, 75, 600),
        Word("0000000002", 31, 80, 617),
    ]
    with pytest.raises(BankParseError, match="row block"):
        parse_mandiri_pages([_mandiri() + other])


def test_mandiri_a_later_page_whose_header_is_spelt_otherwise_is_refused() -> None:
    upper = [
        dataclasses.replace(w, text="ACCOUNT") if w.text == "Account" else w for w in _mandiri()
    ]
    with pytest.raises(BankParseError):
        parse_mandiri_pages([_mandiri(), upper])


# ── Permata ─────────────────────────────────────────────────────────────────

P_DAYS = [
    (
        "03-Dec-2025",
        "10,000,000.00",
        [
            (1, "03-Dec-2025", "427", "3,000,000.00", "0.00", ["PB transfer", "keluar satu"]),
            (2, "03-Dec-2025", "559", "7,500.00", "0.00", ["Adm. bulanan"]),
        ],
        "3,007,500.00",
        "0.00",
    ),
    (
        "05-Dec-2025",
        "6,992,500.00",
        [
            (3, "05-Dec-2025", "374", "0.00", "500,000.00", ["Setoran"]),
            (14, "05-Dec-2025", "374", "0.00", "500,000.00", ["Setoran"]),
        ],
        "0.00",
        "1,000,000.00",
    ),
]


def _permata(days=P_DAYS, closing=("31-Dec-2025", "7,992,500.00"), **kw):
    return permata_page(days, closing=closing, **kw)


def test_permata_derives_each_balance_from_the_printed_day_openings() -> None:
    stmt = parse_permata_pages([_permata()])
    assert (stmt.bank_code, stmt.source_format) == ("bank_permata", "pdf")
    assert (stmt.account_number, stmt.account_name) == (ACCOUNT, NAME)
    t = stmt.transactions
    assert [x.balance for x in t] == [
        Decimal("7000000.00"),
        Decimal("6992500.00"),
        Decimal("7492500.00"),
        Decimal("7992500.00"),
    ]
    assert t[0].posted_at == datetime(2025, 12, 3) and t[0].description == "PB transfer keluar satu"
    assert (t[3].debit, t[3].credit) == (Decimal(0), Decimal("500000.00"))
    assert stmt.opening_balance == Decimal("10000000.00") and stmt.closing_balance == Decimal(
        "7992500.00"
    )
    assert check_running_balance(t) == []
    assert t[1].kind == "bank_fee"
    assert t[0].raw_row["Transaction Code"] == "427"


def test_permata_the_row_number_does_not_leak_into_the_date() -> None:
    # "14" + "05-Dec-2025" is printed as one word
    assert parse_permata_pages([_permata()]).transactions[3].txn_date.day == 5


@pytest.mark.parametrize(
    "mutation",
    [
        "currency",
        "closing",
        "day_total",
        "day_credit_total",
        "opening_chain",
        "no_closing",
        "no_rows",
    ],
)
def test_permata_refuses_what_does_not_add_up(mutation) -> None:
    days, closing, kw = [list(d) for d in P_DAYS], ("31-Dec-2025", "7,992,500.00"), {}
    if mutation == "currency":
        kw["currency"] = "EUR"
    elif mutation == "closing":
        closing = ("31-Dec-2025", "7,992,501.00")
    elif mutation == "day_credit_total":
        days[1][4] = "1,000,001.00"
    elif mutation == "day_total":
        days[0][3] = "3,007,501.00"
    elif mutation == "opening_chain":
        days[1][1] = "6,992,501.00"
    elif mutation == "no_closing":
        closing = None
    elif mutation == "no_rows":
        days = [["03-Dec-2025", "10,000,000.00", [], "0.00", "0.00"]]
        closing = ("31-Dec-2025", "10,000,000.00")
    with pytest.raises(BankParseError):
        parse_permata_pages([_permata([tuple(d) for d in days], closing, **kw)])


def test_permata_a_row_may_continue_on_the_next_page() -> None:
    one = _permata()
    split_at = next(i for i, w in enumerate(one) if w.text == "Ledger" and w.top > 380)
    # page 2 repeats the page head; the second day starts there
    head = [w for w in one if w.top < 340]
    page1 = one[: split_at - 1]
    page2 = head + one[split_at - 1 :]
    stmt = parse_permata_pages([page1, page2])
    assert len(stmt.transactions) == 4


def _permata_with_line(*tokens: str) -> list[list[Word]]:
    """P_DAYS on one page plus one more line under the closing line, at the account label's x."""
    page = _permata()
    y = max(w.top for w in page) + 40
    return [page + [Word(t, 40 + 45 * i, 40 + 45 * i + 40, y) for i, t in enumerate(tokens)]]


@pytest.mark.parametrize(
    "tokens",
    [
        ("ACCOUNT", ":", "0000000002/PT", "CONTOH", "(", "IDR", ")"),
        ("Account", "No.", ":", "0000-0000-02"),
        ("No.", "Rekening", ":", "0000", "0000", "02"),
        ("Account", ":", "0000000001/PT", "CONTOH", "(", "USD", ")"),
    ],
)
def test_permata_another_account_line_in_any_spelling_is_refused(tokens) -> None:
    with pytest.raises(BankParseError, match="more than one account"):
        parse_permata_pages(_permata_with_line(*tokens))


@pytest.mark.parametrize(
    "tokens",
    [
        ("ACCOUNT", ":", "0000-0000-01"),
        ("account", ":", "0000000001", "(", "idr", ")"),
        ("Account", "Information"),
    ],
)
def test_permata_the_same_account_in_any_spelling_reads_as_one_statement(tokens) -> None:
    stmt = parse_permata_pages(_permata_with_line(*tokens))
    assert stmt.account_number == ACCOUNT and len(stmt.transactions) == 4


def test_permata_a_description_that_mentions_another_account_never_refuses_the_file() -> None:
    first, *rest = P_DAYS
    rows = [(*first[2][0][:5], ["PB transfer", "ACCOUNT : 1234567890"]), *first[2][1:]]
    stmt = parse_permata_pages([_permata([(*first[:2], rows, *first[3:]), *rest])])
    assert stmt.account_number == ACCOUNT and len(stmt.transactions) == 4


# ── detection and the entry point ───────────────────────────────────────────


def test_detection_tells_the_two_layouts_apart() -> None:
    assert detect_pdf_format([_mandiri()]) == "mandiri"
    assert detect_pdf_format([_permata()]) == "permata"
    assert detect_pdf_format([[]]) is None


def test_synthetic_mandiri_pdf_end_to_end() -> None:
    stmt, report = parse_and_preview_statement(words_to_pdf([_mandiri()]))
    assert stmt.bank_code == "bank_mandiri" and len(stmt.transactions) == 3
    assert report["total_debit"] == "2525000.00" and report["total_credit"] == "1000000.00"
    assert report["balance_check"] == {"ok": True, "mismatch_positions": []}
    assert preview_statement(words_to_pdf([_mandiri()])) == report


def test_synthetic_permata_pdf_end_to_end() -> None:
    stmt, report = parse_and_preview_statement(words_to_pdf([_permata()]))
    assert stmt.bank_code == "bank_permata" and report["row_count"] == 4
    assert report["opening_balance"] == "10000000.00" and report["closing_balance"] == "7992500.00"


def test_the_csv_path_is_unchanged() -> None:
    text = statement_text([(at(2, 9), "DIRECT DEBIT", "100.00", "0.00")], opening="1000.00")
    stmt, report = parse_and_preview_statement(text.encode())
    assert (stmt.bank_code, stmt.source_format) == ("cimb_niaga", "csv")
    assert report["row_count"] == 1


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4 not really a pdf", b"%PDF-", words_to_pdf([[]]), b"\xff\xfe\x00bad", b"a,b\n1,2\n"],
    # fixed ids: a drawn PDF differs on every run, and parallel workers must collect the same names
    ids=["not-a-pdf", "pdf-header-only", "blank-pdf", "not-utf8", "unknown-csv"],
)
def test_unreadable_or_unknown_files_are_a_plain_refusal(data) -> None:
    with pytest.raises(BankParseError):
        parse_and_preview_statement(data)


def test_refusals_never_echo_what_the_file_says() -> None:
    planted = "PLANTED-MARKER-77"
    for pages in (
        [_mandiri(currency="USD", name=planted, account="0000000009")],
        [_mandiri(total_debit="1.00", name=planted)],
        [_permata(currency="EUR", name=planted)],
        [_permata(closing=("31-Dec-2025", "1.00"), name=planted)],
    ):
        with pytest.raises(BankParseError) as err:
            parse_and_preview_statement(words_to_pdf(pages))
        assert planted not in str(err.value) and "0000000009" not in str(err.value)


def test_extract_pdf_pages_refuses_garbage_and_too_many_pages(monkeypatch) -> None:
    with pytest.raises(BankParseError, match="not a readable PDF"):
        extract_pdf_pages(b"%PDF-1.4 garbage")
    import backend.services.accounting.bank_pdf_readers as mod

    monkeypatch.setattr(mod, "_MAX_PDF_PAGES", 1)
    with pytest.raises(BankParseError, match="too many pages"):
        extract_pdf_pages(words_to_pdf([_mandiri(), _mandiri()]))


# ── through the HTTP handlers ───────────────────────────────────────────────

_ADMIN = {"id": "asya", "email": "asya@example.test", "role": "admin"}


def _post(mock_db_pool, url: str, data: bytes, name: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.app.routers.accounting as accounting_module
    from backend.app.dependencies import get_current_user, get_database_pool

    pool, _ = mock_db_pool
    app = FastAPI()
    app.include_router(accounting_module.router)
    app.dependency_overrides[get_current_user] = lambda: _ADMIN
    app.dependency_overrides[get_database_pool] = lambda: pool
    client = TestClient(app, raise_server_exceptions=False)
    return client.post(url, files={"file": (name, data, "application/pdf")})


def test_the_preview_endpoint_reads_a_pdf(mock_db_pool) -> None:
    resp = _post(
        mock_db_pool,
        "/api/crm/accounting/bank-statement/preview",
        words_to_pdf([_mandiri()]),
        "statement.PDF",
    )
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 3 and resp.json()["account"]["number"] == ACCOUNT


def test_the_import_endpoint_refuses_an_unknown_pdf_with_a_fixed_message(mock_db_pool) -> None:
    resp = _post(
        mock_db_pool,
        "/api/crm/accounting/bank-statement/import",
        words_to_pdf([[Word("hello", 10, 10, 20)]]),
        "statement.pdf",
    )
    assert resp.status_code == 422 and "bank statement this page reads" in resp.json()["detail"]
    _, conn = mock_db_pool
    conn.execute.assert_not_called()


# ── BNI ─────────────────────────────────────────────────────────────────────

# opening 1,000,000.00; credit 500,000.00, debit 120,000.00, fee 25,000.00 -> 1,355,000.00
B_ROWS = [
    ("07/11/2025", "10.00.58", "906849", "Setoran masuk", "500,000.00", "C", "1,500,000.00"),
    ("13/11/2025", "10.58.31", "985452", "Transfer keluar", "120,000.00", "D", "1,380,000.00"),
    ("13/11/2025", "00.00.00", "985452", "Biaya adm rek", "25,000.00", "D", "1,355,000.00"),
]


def _bni(rows=B_ROWS, **kw):
    args = {
        "beginning": "1,000,000.00",
        "total_debit": "145,000.00",
        "total_credit": "500,000.00",
    } | kw
    return bni_page(rows, **args)


def test_bni_inquiry_reads_exact_rows_with_time_and_journal() -> None:
    stmt = parse_bni_pages([_bni()])
    assert (stmt.bank_code, stmt.source_format) == ("bni", "pdf")
    assert (stmt.account_number, stmt.account_name) == (ACCOUNT, NAME)
    t = stmt.transactions
    assert [x.posted_at for x in t] == [
        datetime(2025, 11, 7, 10, 0, 58),
        datetime(2025, 11, 13, 10, 58, 31),
        datetime(2025, 11, 13),
    ]
    assert (t[0].debit, t[0].credit, t[0].balance) == (
        Decimal(0),
        Decimal("500000.00"),
        Decimal("1500000.00"),
    )
    assert t[1].description == "Transfer keluar" and t[0].raw_row == {"Journal No.": "906849"}
    assert stmt.opening_balance == Decimal("1000000.00") and stmt.closing_balance == Decimal(
        "1355000.00"
    )
    assert check_running_balance(t) == [] and t[2].kind == "bank_fee"


def test_bni_a_later_stamp_in_file_order_is_not_a_reason_to_reorder() -> None:
    # the 00.00.00 fee follows a 10:58 row: the balance chain decides, not the clock
    assert [x.balance for x in parse_bni_pages([_bni()]).transactions][-1] == Decimal("1355000.00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"currency": "USD"},
        {"total_debit": "145,001.00"},
        {"total_credit": "500,001.00"},
        {"beginning": "1,000,001.00"},
    ],
    ids=["not_idr", "debit_total", "credit_total", "beginning"],
)
def test_bni_refuses_what_does_not_add_up(kwargs) -> None:
    with pytest.raises(BankParseError):
        parse_bni_pages([_bni(**kwargs)])


def test_bni_a_blank_currency_is_read_a_foreign_one_is_not() -> None:
    assert parse_bni_pages([_bni(currency="")]).bank_code == "bni"
    with pytest.raises(BankParseError, match="IDR"):
        parse_bni_pages([_bni(currency="EUR")])


def test_bni_an_unreadable_amount_is_refused_not_guessed() -> None:
    rows = [(*B_ROWS[0][:4], "500,0000.00", *B_ROWS[0][5:]), *B_ROWS[1:]]
    with pytest.raises(BankParseError):
        parse_bni_pages([_bni(rows)])


def test_bni_detection_and_synthetic_pdf_end_to_end() -> None:
    assert detect_pdf_format([_bni()]) == "bni"
    stmt, report = parse_and_preview_statement(words_to_pdf([_bni()]))
    assert stmt.bank_code == "bni" and report["row_count"] == 3
    assert report["total_debit"] == "145000.00" and report["balance_check"]["ok"] is True


_LONG = [
    ("07/11/2025", "10.00.58", "906849", "ALPHA\nA1\nA2\nA3", "500,000.00", "C", "1,500,000.00"),
    ("13/11/2025", "10.58.31", "985452", "BETA", "120,000.00", "D", "1,380,000.00"),
    ("13/11/2025", "00.00.00", "985452", "GAMMA\nG1", "25,000.00", "D", "1,355,000.00"),
]


def test_bni_a_long_description_stays_whole_next_to_a_short_one() -> None:
    stmt = parse_bni_pages([_bni(_LONG)])
    assert [t.description for t in stmt.transactions] == ["ALPHA A1 A2 A3", "BETA", "GAMMA G1"]


def test_bni_a_date_that_falls_outside_its_row_number_block_is_refused() -> None:
    page = _bni(_LONG)
    moved = [
        dataclasses.replace(w, top=w.top - 12) if w.text == "13/11/2025" and w.top > 250 else w
        for w in page
    ]
    with pytest.raises(BankParseError, match="row block"):
        parse_bni_pages([moved])


def test_bni_text_above_the_first_row_number_is_refused() -> None:
    page = _bni()
    page.append(Word("orphan", 270, 300, 214.5))  # a description line above row 1's number
    with pytest.raises(BankParseError, match="row block"):
        parse_bni_pages([page])


def test_bni_the_printed_totals_are_checked_against_the_rows_side_by_side() -> None:
    # both totals up by one leave beginning + credits - debits unchanged: only the comparison of
    # the rows with each printed total can refuse it, and the message names each side that fails
    with pytest.raises(BankParseError, match=r"\(debit and credit\)"):
        parse_bni_pages([_bni(total_debit="145,001.00", total_credit="500,001.00")])


def test_bni_the_account_statement_print_is_refused_with_a_pointer_to_the_inquiry() -> None:
    with pytest.raises(BankParseError, match="download the Transaction Inquiry"):
        parse_bni_pages([_bni(title="ACCOUNT STATEMENT")])
    with pytest.raises(BankParseError, match="download the Transaction Inquiry"):
        parse_and_preview_statement(words_to_pdf([_bni(title="ACCOUNT STATEMENT")]))


def _bni_two_pages(second_account: str = ACCOUNT, **kw):
    first = _bni(B_ROWS[:2])
    second = _bni(B_ROWS[2:], start_no=3, table_header=False, account=second_account, **kw)
    return [first, second]


def test_bni_a_second_page_continues_the_same_account() -> None:
    stmt = parse_bni_pages(_bni_two_pages())
    assert len(stmt.transactions) == 3 and stmt.account_number == ACCOUNT


def test_bni_a_second_page_printing_another_account_is_refused() -> None:
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages(_bni_two_pages("0000000002"))
    with pytest.raises(BankParseError, match="more than one account"):
        parse_and_preview_statement(words_to_pdf(_bni_two_pages("0000000002")))


def test_bni_a_second_account_block_on_the_same_page_is_refused() -> None:
    first = _bni(B_ROWS[:2])
    below = [
        dataclasses.replace(w, top=w.top + 400)
        for w in _bni(B_ROWS[2:], start_no=3, table_header=False, account="0000000002")
    ]
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages([first + below])


def test_bni_a_second_account_line_spelt_differently_is_refused() -> None:
    first = _bni(B_ROWS)
    y = max(w.top for w in first) + 40
    other = [
        Word("Account", 97, 130, y),
        Word("No.", 133, 150, y),
        Word(":", 202, 205, y),
        Word("0000000002", 213, 270, y),
    ]
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages([first + other])


def test_bni_a_page_printing_the_same_account_in_another_currency_is_refused() -> None:
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages(_bni_two_pages(currency="USD"))


def _bni_with_line(*tokens: str) -> list[list[Word]]:
    """B_ROWS on one page plus one more line under the rows, starting at the account block's x."""
    page = _bni(B_ROWS)
    y = max(w.top for w in page) + 40
    return [page + [Word(t, 97 + 45 * i, 97 + 45 * i + 40, y) for i, t in enumerate(tokens)]]


@pytest.mark.parametrize(
    "tokens",
    [
        ("ACCOUNT", ":", "0000000002/PT", "CONTOH", "(", "IDR", ")"),
        ("account", "no.", ":", "0000000002"),
        ("Account", "Number", "0000000002"),
        ("Acct.", "No", ":", "0000-0000-02"),
        ("A/C", ":", "0000.0000.02"),
        ("No.", "Rekening", ":", "0000", "0000", "02"),
        ("NOMOR", "REKENING", "0000000002"),
        ("Account", ":", "0000000001/PT", "CONTOH", "(", "USD", ")"),
        ("Account0000000002/PT", "CONTOH", "(", "IDR", ")"),
        ("ACCOUNT:0000000002",),
    ],
)
def test_bni_another_account_line_in_any_spelling_is_refused(tokens) -> None:
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages(_bni_with_line(*tokens))


@pytest.mark.parametrize(
    "tokens",
    [
        ("ACCOUNT", ":", "0000-0000-01"),
        ("Account", "No.", ":", "0000000001"),
        ("account", ":", "0000", "0000", "01", "(", "idr", ")"),
        ("Account", "Information"),
        ("account", ":", "0000000001", "(", ")"),
        ("Account0000000001",),
    ],
)
def test_bni_the_same_account_in_any_spelling_reads_as_one_statement(tokens) -> None:
    stmt = parse_bni_pages(_bni_with_line(*tokens))
    assert stmt.account_number == ACCOUNT and len(stmt.transactions) == 3


def test_bni_a_blank_currency_reads_as_idr_on_every_account_line() -> None:
    # the readers accept a blank currency as IDR: a blank and an IDR line name the same currency
    for first, later in (("", "IDR"), ("IDR", "")):
        page = _bni(B_ROWS, currency=first)
        y = max(w.top for w in page) + 40
        line = ["Account", ":", "0000000001", "(", *([later] if later else []), ")"]
        extra = [Word(t, 97 + 45 * i, 97 + 45 * i + 40, y) for i, t in enumerate(line)]
        assert len(parse_bni_pages([page + extra]).transactions) == 3
    page = _bni(B_ROWS, currency="")
    y = max(w.top for w in page) + 40
    usd = [
        Word(t, 97 + 45 * i, 97 + 45 * i + 40, y)
        for i, t in enumerate(["Account", ":", "0000000001", "(", "USD", ")"])
    ]
    with pytest.raises(BankParseError, match="more than one account"):
        parse_bni_pages([page + usd])


def test_bni_a_description_that_mentions_another_account_never_refuses_the_file() -> None:
    # the mention sits in the description column, right of the journal column: never an account
    # line (row 3, away from the page-chrome lines, whose own filter is not under test here)
    rows = [*B_ROWS[:2], (*B_ROWS[2][:3], "TRF KE\nACCOUNT : 1234567890", *B_ROWS[2][4:])]
    stmt = parse_bni_pages([_bni(rows)])
    assert stmt.account_number == ACCOUNT and len(stmt.transactions) == 3


def test_bni_a_description_carried_over_a_page_break_never_lands_on_another_row() -> None:
    rows = [(*B_ROWS[0][:3], "ALPHA\nA1\nA2\nA3", *B_ROWS[0][4:]), *B_ROWS[1:]]
    # the same rows, with the last line of row 1 printed at the top of page 2, above row 2's number
    first = [w for w in _bni(rows[:1]) if w.text != "A3"]
    second = _bni(rows[1:], start_no=2, table_header=False)
    carried = [*second, Word("A3", 263, 220, 214.0)]
    with pytest.raises(BankParseError, match="row block"):
        parse_bni_pages([first, carried])
    # control: the page break between two whole rows reads fine
    whole = parse_bni_pages([_bni(rows[:1]), second])
    assert [t.description for t in whole.transactions][0] == "ALPHA A1 A2 A3"
