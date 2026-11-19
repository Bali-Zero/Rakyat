"""Mandiri and Permata statement readers, and the one upload entry point (invented data only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from backend.services.accounting.bank_parser import BankParseError, check_running_balance
from backend.services.accounting.bank_pdf_readers import (
    Word,
    detect_pdf_format,
    extract_pdf_pages,
    parse_and_preview_statement,
    parse_mandiri_pages,
    parse_permata_pages,
    preview_statement,
)
from backend.tests.fixtures.bank_pdf_builder import (
    ACCOUNT,
    NAME,
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
        {"closing": "8,475,001.00"},
        {"opening": "10,000,001.00"},
    ],
    ids=["not_idr", "totals", "closing", "opening"],
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
    ["currency", "closing", "day_total", "opening_chain", "no_closing", "no_rows"],
)
def test_permata_refuses_what_does_not_add_up(mutation) -> None:
    days, closing, kw = [list(d) for d in P_DAYS], ("31-Dec-2025", "7,992,500.00"), {}
    if mutation == "currency":
        kw["currency"] = "EUR"
    elif mutation == "closing":
        closing = ("31-Dec-2025", "7,992,501.00")
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
