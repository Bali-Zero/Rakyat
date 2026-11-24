"""Regression coverage for Permata Transaction History exports (invented data only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from backend.services.accounting.bank_parser import BankParseError
from backend.services.accounting.bank_pdf_readers import Word, parse_and_preview_statement
from backend.tests.fixtures.bank_pdf_builder import words_to_pdf


def _word(text: str, x0: float, top: float) -> Word:
    return Word(text, x0, x0 + 4.8 * len(text), top)


def _amount(text: str, top: float) -> Word:
    return Word(text, 571.0 - 4.8 * len(text), 571.0, top)


def _history_page(
    rows: list[tuple[str, str, str, str, str]],
    *,
    page: int,
    pages: int = 2,
    opening: str = "100.00",
    total_debit: str = "11.00",
    total_credit: str = "5.50",
    closing: str = "94.50",
    account: str = "0000-0000-999",
    title: str = "TRANSACTION HISTORY",
    include_summary: bool = True,
) -> list[Word]:
    """One invented export page, retaining the report's column geometry and chrome."""
    words = [_word(title, 24, 28), _word("24 Jan 2026 | 11:22:33", 24, 45)]
    summary = [
        ("Account Name", "PT SYNTHETIC", "Opening Balance", opening),
        ("Account No.", account, "Total Debit", total_debit),
        ("Currency", "IDR", "Total Credit", total_credit),
        ("Period", "01 Oct 2026 - 03 Oct 2026", "Closing Balance", closing),
    ]
    if include_summary:
        for index, (left, value, right, amount) in enumerate(summary):
            y = 72.0 + 18.0 * index
            words += [
                _word(left, 24, y),
                _word(value, 142, y),
                _word(right, 320, y),
                _amount(amount, y),
            ]
    # The PDF has a three-line header; Cheque No. is on the final header line.
    words += [
        _word("Transaction", 24, 164),
        _word("Value", 78, 164),
        _word("Transaction Reference No./", 132, 164),
        _word("Description", 248, 164),
        _word("Amount", 545, 164),
        _word("Date", 24, 172),
        _word("Date", 78, 172),
        _word("Customer Reference Detail/", 132, 172),
        _word("Cheque No.", 132, 180),
    ]
    for index, (posted, effective, reference, description, amount) in enumerate(rows):
        y = 204.0 + 20.0 * index
        words += [
            _word(posted, 24, y),
            _word(effective, 78, y),
            _word(reference, 132, y),
            _word(description, 248, y),
            _amount(amount, y),
        ]
    words.append(_word(f"Page {page} of {pages}", 470, 790))
    return words


def _parse(*pages: list[Word]):
    return parse_and_preview_statement(words_to_pdf(list(pages)))


def test_history_prints_newest_first_but_returns_oldest_first_and_uses_transaction_date() -> None:
    newest = _history_page(
        [
            ("03 Oct 2026", "30 Sep 2026", "REF-3-", "credit narration-", "5.50"),
            ("02 Oct 2026", "02 Oct 2026", "REF-2", "second debit", "-0.99"),
        ],
        page=1,
    )
    oldest = _history_page(
        [("01 Oct 2026", "01 Oct 2026", "REF-1", "split debit", "-10.01")],
        page=2,
        include_summary=False,
    )

    statement, preview = _parse(newest, oldest)

    assert [txn.posted_at for txn in statement.transactions] == [
        datetime(2026, 10, 1),
        datetime(2026, 10, 2),
        datetime(2026, 10, 3),
    ]
    assert [(txn.debit, txn.credit) for txn in statement.transactions] == [
        (Decimal("10.01"), Decimal("0")),
        (Decimal("0.99"), Decimal("0")),
        (Decimal("0"), Decimal("5.50")),
    ]
    assert statement.account_number == "00000000999"
    assert statement.transactions[-1].raw_row["Eff Date"] == "30 Sep 2026"
    assert statement.transactions[-1].raw_row["Ref No"] == "REF-3-"
    assert statement.transactions[-1].description == "credit narration-"
    assert preview["total_debit"] == "11.00" and preview["total_credit"] == "5.50"


def test_history_accepts_a_real_or_split_negative_amount() -> None:
    page = _history_page(
        [("01 Oct 2026", "01 Oct 2026", "REF-1", "ordinary debit", "10.01")],
        page=1,
        pages=1,
        total_debit="10.01",
        total_credit="0.00",
        closing="89.99",
    )
    amount = next(word for word in page if word.text == "10.01" and word.top > 200)
    page.remove(amount)
    page += [_word("-", amount.x0 - 6, amount.top), amount]

    statement, _ = _parse(page)

    assert (statement.transactions[0].debit, statement.transactions[0].credit) == (
        Decimal("10.01"),
        Decimal("0"),
    )


def test_history_refuses_cent_exact_control_total_mismatch() -> None:
    page = _history_page(
        [("01 Oct 2026", "01 Oct 2026", "REF-1", "debit", "-10.01")],
        page=1,
        pages=1,
        total_debit="10.00",
        total_credit="0.00",
        closing="89.99",
    )

    with pytest.raises(BankParseError):
        _parse(page)


def test_history_refuses_swapped_transaction_and_value_date_headings() -> None:
    page = _history_page(
        [("01 Oct 2026", "01 Oct 2026", "REF-1", "debit", "-10.01")],
        page=1,
        pages=1,
        total_debit="10.01",
        total_credit="0.00",
        closing="89.99",
    )
    for i, word in enumerate(page):
        if word.text == "Transaction" and word.x0 == 24:
            page[i] = _word("Value", 24, word.top)
        if word.text == "Value" and word.x0 == 78:
            page[i] = _word("Transaction", 78, word.top)

    with pytest.raises(BankParseError):
        _parse(page)


def test_history_refuses_a_missing_page_or_an_extra_account_header() -> None:
    row = ("01 Oct 2026", "01 Oct 2026", "REF", "ordinary debit", "-1.00")
    first = _history_page(
        [row], page=1, pages=2, total_debit="2.00", total_credit="0.00", closing="98.00"
    )
    with pytest.raises(BankParseError):
        _parse(first)
    other = _history_page([row], page=2, pages=2, account="0000-0000-888")
    with pytest.raises(BankParseError):
        _parse(first, other)


def test_history_refuses_status_exports_and_truncated_summaries() -> None:
    row = ("01 Oct 2026", "01 Oct 2026", "REF", "ordinary debit", "-1.00")

    def page():
        return _history_page(
            [row], page=1, pages=1, total_debit="1.00", total_credit="0.00", closing="99.00"
        )

    status = [
        _word("TRANSACTION STATUS", w.x0, w.top) if w.text == "TRANSACTION HISTORY" else w
        for w in page()
    ]
    with pytest.raises(BankParseError):
        _parse(status)

    with pytest.raises(BankParseError):
        _parse([w for w in page() if w.text != "Closing Balance"])
