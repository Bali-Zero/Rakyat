"""Build invented Mandiri and Permata statement pages as extracted words (no real bank data).

The coordinates follow the layout of the two JasperReports exports, so the readers are tested
over the same word geometry they see on a real file. `words_to_pdf` draws the words into a real
PDF (reportlab) for the end-to-end tests.
"""

from __future__ import annotations

import io
import re

from backend.services.accounting.bank_pdf_readers import Word

NAME = "PT CONTOH SATU"
ACCOUNT = "0000000001"
_PAGE_H = 842.0
_AMOUNT = re.compile(r"-?[\d,]+\.\d{2}")


def _w(text: str, x0: float, top: float) -> Word:
    return Word(text, x0, x0 + 5.0 * len(text), top)


def _amt(text: str, right: float, top: float) -> Word:
    return Word(text, right - 5.0 * len(text), right, top)


def money(value: object) -> str:
    return f"{value:,.2f}"


# (date "16 Nov 2026", time "10:00:00", remark lines, debit, credit, balance)
MandiriRow = tuple[str, str, list[str], str, str, str]


def mandiri_page(
    rows: list[MandiriRow],
    *,
    opening: str,
    closing: str,
    total_debit: str,
    total_credit: str,
    account: str = ACCOUNT,
    name: str = NAME,
    currency: str = "IDR",
    gap: float | None = None,
) -> list[Word]:
    words = [
        _w("Account", 32, 27),
        _w("Statement", 75, 27),
        _w("Account", 31, 101),
        _w("Statement", 74, 101),
        _w("Summary", 125, 101),
        _w("Account", 31, 129),
        _w("No.", 62, 129),
        _w("Account", 213, 129),
        _w("Name", 244, 129),
        _w("Alias", 395, 129),
        _w(account, 31, 146),
    ]
    words += [_w(p, 213 + 40 * i, 146) for i, p in enumerate(name.split())]
    words += [
        _w("Period", 31, 170),
        _w("Currency", 213, 170),
        _w("Branch", 395, 170),
        _w("01", 31, 186),
        _w("Nov", 42, 186),
        _w("2026", 59, 186),
        _w("-", 79, 186),
        _w("17", 84, 186),
        _w("Nov", 95, 186),
        _w("2026", 113, 186),
        _w(currency, 213, 186),
        _w("Opening", 31, 209),
        _w("Balance", 63, 209),
        _w("No.", 213, 209),
        _w("of", 228, 209),
        _w("Debit", 237, 209),
        _w("Total", 395, 209),
        _w("Amount", 415, 209),
        _w("Debited", 445, 209),
        _w(opening, 31, 225),
        _w("1", 213, 225),
        _w(total_debit, 395, 225),
        _w("Closing", 31, 247),
        _w("Balance", 60, 247),
        _w("No.", 213, 247),
        _w("of", 228, 247),
        _w("Credit", 237, 247),
        _w("Total", 395, 247),
        _w("Amount", 415, 247),
        _w("Credited", 445, 247),
        _w(closing, 31, 262),
        _w("1", 213, 262),
        _w(total_credit, 395, 262),
        _w("Posting", 35, 292),
        _w("Date", 67, 292),
        _w("Remark", 120, 292),
        _w("Reference", 240, 292),
        _w("No.", 281, 292),
        _w("Debit", 362, 292),
        _w("Credit", 449, 292),
        _w("Balance", 533, 292),
    ]
    # Real Kopra layout: remark lines 9 pt apart, the amounts on the middle remark line, the date
    # 5 pt above them and the time 5 pt below, and about 30 pt of empty space before the next row.
    y = 308.0
    between = 32.0 if gap is None else gap
    for day, time, remark, debit, credit, balance in rows:
        n = max(len(remark), 1)
        amount_y = y + 9 * (n // 2)
        d, mon, year = day.split()
        words += [
            _w(d, 34, amount_y - 5),
            _w(mon, 45, amount_y - 5),
            _w(year + ",", 62, amount_y - 5),
            _w(time, 34, amount_y + 5),
            _w("-", 239, amount_y),
        ]
        words += [
            _amt(debit, 395, amount_y),
            _amt(credit, 480, amount_y),
            _amt(balance, 575, amount_y),
        ]
        for k, line in enumerate(remark):
            for j, token in enumerate(line.split()):
                words.append(_w(token, 119 + 30 * j, y + 9 * k))
        y = max(y + 9 * (n - 1), amount_y + 5) + between
    words += [
        _w("For", 158, 799),
        _w("further", 181, 799),
        _w("questions,", 215, 799),
        _w("Page", 499, 799),
    ]
    return words


# a Permata day: (date "03-Dec-2025", opening balance, [rows], total debit, total credit)
# a row: (no, date, code, debit, credit, [description lines])
PermataRow = tuple[int, str, str, str, str, list[str]]


def permata_page(
    days: list[tuple[str, str, list[PermataRow], str, str]],
    *,
    closing: tuple[str, str] | None,
    currency: str = "IDR",
    account: str = ACCOUNT,
    name: str = NAME,
) -> list[Word]:
    words = [
        _w("Transaction", 344, 136),
        _w("Inquiry", 444, 136),
        _w("Inquiry", 40, 210),
        _w("Time", 79, 210),
        _w(":", 286, 210),
        _w("Account", 40, 250),
        _w(":", 286, 250),
        _w(f"{account}/{name.split()[0]}", 308, 252),
    ]
    words += [_w(p, 407 + 45 * i, 252) for i, p in enumerate(name.split()[1:])]
    words += [_w("(", 474, 252), _w(currency, 480, 252), _w(")", 500, 252)]
    words += [
        _w("Period", 40, 270),
        _w(":", 286, 270),
        _w("01-Dec-2025", 308, 272),
        _w("31-Dec-2025", 410, 272),
    ]
    words += [
        _w("Transaction", 212, 320),
        _w("No.", 34, 326),
        _w("Post", 69, 326),
        _w("Date", 94, 326),
        _w("Debit", 652, 326),
        _w("Credit", 761, 326),
        _w("Code", 228, 332),
    ]
    y = 346.0
    for day, opening, rows, total_debit, total_credit in days:
        words += [
            _w(t, x, y)
            for t, x in (("Opening", 30), ("Ledger", 71), ("Balance", 104), ("per", 143))
        ]
        words += [_w(day, 197, y), _amt(opening, 830, y)]
        y += 18
        for no, date, code, debit, credit, desc in rows:
            words += [
                _w(f"{no}{date}", 44, y),
                _w(date, 130, y),
                _w(code, 210, y),
                _w("0000000000", 270, y),
            ]
            words += [_amt(debit, 720, y), _amt(credit, 830, y)]
            for k, line in enumerate(desc):
                words += [_w(t, 505 + 40 * j, y + 11 * k) for j, t in enumerate(line.split())]
            y += 11 * max(len(desc), 1) + 10
        words += [_w("Total", 30, y), _amt(total_debit, 720, y + 4), _amt(total_credit, 830, y + 4)]
        y += 30
    if closing is not None:
        words += [
            _w(t, x, y)
            for t, x in (("Closing", 30), ("Ledger", 66), ("Balance", 100), ("per", 139))
        ]
        words += [_w(closing[0], 197, y), _amt(closing[1], 830, y)]
    return words


def words_to_pdf(pages: list[list[Word]]) -> bytes:
    """Draw words at their coordinates into a real PDF (amounts right-aligned at x1)."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(900, _PAGE_H))
    pdf.setFont("Helvetica", 7)
    for words in pages:
        for w in words:
            y = _PAGE_H - w.top - 7
            if _AMOUNT.fullmatch(w.text):
                pdf.drawRightString(w.x1, y, w.text)
            else:
                pdf.drawString(w.x0, y, w.text)
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


# ── BNI ─────────────────────────────────────────────────────────────────────
def _n(text: str, x0: float, top: float) -> Word:  # Helvetica 7pt is ~3.6 pt a character
    return Word(text, x0, x0 + 3.6 * len(text), top)


def _na(text: str, right: float, top: float) -> Word:
    return Word(text, right - 3.6 * len(text), right, top)


# a row: (date "07/11/2025", time "10.00.58", journal, description, amount, flag, balance or None)
BniRow = tuple[str, str, str, str, str, str, str | None]


def bni_page(
    rows: list[BniRow],
    *,
    beginning: str,
    total_debit: str,
    total_credit: str,
    currency: str = "IDR",
    account: str = ACCOUNT,
    name: str = NAME,
    variant: str = "inquiry",
    ending: str | None = None,
    counts: tuple[int, int] = (0, 0),
) -> list[Word]:
    """'inquiry' prints the beginning balance and totals in the header, times on the row's flag
    line; 'statement' prints a lone balance before the first row, and ending, counts and totals
    after the last."""
    words = [_n("Account", 257, 110), _n("Information", 302, 110)]
    cur = f"( {currency} )" if currency else "( )"
    head = [_n("Account", 97, 156), _n(":", 202, 156), _n(f"{account}/{name.split()[0]}", 213, 156)]
    head += [_n(p, 300 + 40 * i, 156) for i, p in enumerate(name.split()[1:] + cur.split())]
    words += head
    if variant == "inquiry":
        words += [_n("TRANSACTION", 246, 130), _n("INQUIRY", 327, 130)]
        for label, value, y in (
            ("Beginning Balance", beginning, 176),
            ("Total Debit", total_debit, 188),
            ("Total Credit", total_credit, 198),
        ):
            words += [_n(t, 97 + 40 * i, y) for i, t in enumerate(label.split())] + [
                _n(":", 202, y)
            ]
            words.append(_n(value, 213, y))
    else:
        words += [_n("ACCOUNT", 231, 88), _n("STATEMENT", 282, 88)]
    top = 210.0
    words += [
        _n("Post", 129, top),
        _n("Date", 150, top),
        _n("Journal", 224, top),
        _n("Amount", 410, top),
        _n("Db/Cr", 451, top),
        _n("Balance", 486, top),
    ]
    if variant == "inquiry":
        words += [_n("No.", 250, top), _n("No.", 97, top)]
    y = top + 8
    if variant == "statement":
        words.append(_n(beginning, 480, y))
        y += 12
    # inquiry: row number, then the description from the number's line downwards (top aligned);
    # statement: no number, and every cell centred on the flag line, 14 pt between two rows
    for i, (date, time, journal, desc, amount, flag, balance) in enumerate(rows):
        lines = desc.split("\n")
        extent = 9 * (len(lines) - 1)
        if variant == "statement":
            first = y + (14 if i else 0)
            anchor = first + extent / 2
            y = first + extent + 9
        else:
            first = anchor = y
            words.append(_n(str(i + 1), 96, anchor))
            y = anchor + max(30, extent + 12)
        words += [_n(date, 110, anchor), _n(time, 152, anchor), _n(journal, 223, anchor)]
        for k, line in enumerate(lines):
            words += [_n(t, 263 + 30 * j, first + 9 * k) for j, t in enumerate(line.split())]
        words += [_na(amount, 445, anchor), _n(flag, 456, anchor)]
        if balance is not None:
            words.append(_n(balance, 480, anchor))
    if variant == "statement":
        words += [
            _n("Ending", 162, y),
            _n("Balance", 186, y),
            _n(":", 213, y),
            _na(ending or "", 500, y),
        ]
        words += [
            _n("Total", 174, y + 14),
            _n("Debet", 192, y + 14),
            _n(":", 213, y + 14),
            _n(str(counts[0]), 225, y + 14),
            _n(total_debit, 272, y + 14),
        ]
        words += [
            _n("Total", 174, y + 28),
            _n("Credit", 192, y + 28),
            _n(":", 213, y + 28),
            _n(str(counts[1]), 225, y + 28),
            _n(total_credit, 272, y + 28),
        ]
    return words
