"""Readers for the two PDF bank statements the agency's other companies download.

One upload on the Accounting page, three formats, detected from the file content:

  * CIMB Niaga OCTO Biz "Account Statement Summary" CSV   (bank_parser.py)
  * Bank Mandiri Kopra "Account Statement" PDF (JasperReports)
  * Bank Permata "Transaction Inquiry" PDF (JasperReports)

Every reader returns the same `ParsedStatement` the CIMB reader returns (oldest row first,
exact Decimal debit/credit/balance, the account number as printed), so the preview report and
`import_statement` are shared. The readers are pure functions over extracted words (`Word`),
so they are tested without a PDF; `extract_pdf_pages` is the only function that opens one.

Only IDR statements are read. A file that does not add up (its own opening + credits - debits
against its own closing, its printed totals, the running balance) is refused. As everywhere in
the accounting code, no message carries content read from the file (spec I9).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from backend.services.accounting.bank_parser import (
    BankParseError,
    ParsedStatement,
    ParsedTxn,
    _total,
    build_preview_report,
    parse_cimb_statement,
)

_MAX_PDF_PAGES = 60
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_AMOUNT_RE = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")
_NOT_IDR = "only IDR statements are read: this statement is in another currency"
_NO_ROWS = "statement has no transactions"


@dataclass(frozen=True)
class Word:
    """One extracted word: its text, horizontal extent and top edge (points, page origin top-left)."""

    text: str
    x0: float
    x1: float
    top: float


def extract_pdf_pages(data: bytes) -> list[list[Word]]:
    """The words of every page. JasperReports draws some columns twice on the same spot, so
    duplicated characters are removed first. Any failure is a fixed-text refusal."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > _MAX_PDF_PAGES:
                raise BankParseError("statement has too many pages")
            pages = [
                [
                    Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]))
                    for w in page.dedupe_chars().extract_words()
                ]
                for page in pdf.pages
            ]
    except BankParseError:
        raise
    except Exception:  # pdfplumber/pdfminer raise many types; their text can quote the file
        raise BankParseError("file is not a readable PDF") from None
    if not any(pages):
        raise BankParseError("file is not a readable PDF")
    return pages


# ── shared helpers ──────────────────────────────────────────────────────────


def _lines(words: list[Word], tol: float = 3.0) -> list[list[Word]]:
    """Words grouped into visual lines (top within `tol` of the line's first word), left to right."""
    out: list[list[Word]] = []
    start = 0.0
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if out and w.top - start <= tol:
            out[-1].append(w)
        else:
            out.append([w])
            start = w.top
    return [sorted(line, key=lambda w: w.x0) for line in out]


def _text(words: list[Word]) -> str:
    return " ".join(w.text for w in sorted(words, key=lambda w: (round(w.top / 3), w.x0)))


def _amount(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _month_date(day: str, mon: str, year: str) -> date:
    try:
        return date(int(year), _MONTHS[mon.lower()], int(day))
    except (KeyError, ValueError):
        raise BankParseError("a date in the statement is not a real date") from None


def _kind(description: str, direction: str) -> str:
    """Row kind from the remark; the PDFs carry no bank payment type. Unknown -> 'other'."""
    d = description.lower()
    if direction == "debit" and ("biaya adm" in d or "adm." in d or "admin" in d):
        return "bank_fee"
    if direction == "credit" and ("bunga" in d or "interest" in d):
        return "interest"
    return "other"


def _txn(
    posted_at: datetime,
    description: str,
    debit: Decimal,
    credit: Decimal,
    balance: Decimal,
    raw_row: dict[str, str],
) -> ParsedTxn:
    direction = "credit" if credit > 0 else "debit"
    return ParsedTxn(
        txn_date=posted_at.date(),
        description=description,
        amount_idr=int((credit if credit > 0 else debit).to_integral_value()),
        direction=direction,
        running_balance_idr=int(balance.to_integral_value()),
        raw_row=raw_row,
        posted_at=posted_at,
        debit=debit,
        credit=credit,
        balance=balance,
        kind=_kind(description, direction) if debit > 0 or credit > 0 else "other",
    )


def detect_pdf_format(pages: list[list[Word]]) -> str | None:
    """'mandiri' | 'permata' | None, from the labels printed on the first page."""
    text = " ".join(w.text for w in pages[0]) if pages else ""
    if "Account Statement Summary" in text and "Posting Date" in text:
        return "mandiri"
    if "Transaction Inquiry" in text and "Ledger Balance" in text:
        return "permata"
    return None


# ── Bank Mandiri (Kopra) ────────────────────────────────────────────────────

_MANDIRI_KEPT = ("Reference No.",)
_REMARK_X, _REF_X, _AMOUNT_X = 100.0, 235.0, 300.0
_DEBIT_CREDIT_SPLIT, _CREDIT_BALANCE_SPLIT = 437.0, 527.0  # right edges of the three columns
_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}")


@dataclass
class _MandiriHeader:
    account: str
    name: str
    currency: str
    opening: Decimal
    closing: Decimal
    total_debit: Decimal
    total_credit: Decimal


@dataclass
class _MandiriRow:
    top: float
    posted_at: datetime
    debit: Decimal
    credit: Decimal
    balance: Decimal
    reference: str
    remark: str


def _value_line(lines: list[list[Word]], first: str, second: str | None = None) -> list[Word]:
    for i, line in enumerate(lines[:-1]):
        head = line[0]
        if (
            head.text == first
            and head.x0 < 100
            and (second is None or (len(line) > 1 and line[1].text == second))
        ):
            return lines[i + 1]
    raise BankParseError("statement header is incomplete")


def _column(line: list[Word], lo: float, hi: float) -> list[Word]:
    return [w for w in line if lo <= w.x0 < hi]


def _mandiri_header(words: list[Word]) -> _MandiriHeader:
    lines = _lines(words)
    acct = _value_line(lines, "Account", "No.")
    period = _value_line(lines, "Period")
    opening = _value_line(lines, "Opening", "Balance")
    closing = _value_line(lines, "Closing", "Balance")
    try:
        number = "".join(w.text for w in _column(acct, 0, 205))
        if not number.isdigit():
            raise BankParseError("statement header has no account number")
        o, c = _column(opening, 0, 205), _column(closing, 0, 205)
        td, tc = _column(opening, 390, 9999), _column(closing, 390, 9999)
        if not (len(o) == len(c) == len(td) == len(tc) == 1):
            raise BankParseError("statement header is incomplete")
        values = [x[0].text for x in (o, c, td, tc)]
        if not all(_AMOUNT_RE.fullmatch(v) for v in values):
            raise BankParseError("statement header amounts are not plain amounts")
        return _MandiriHeader(
            account=number,
            name=_text(_column(acct, 205, 390)),
            currency=_text(_column(period, 205, 390)),
            opening=_amount(values[0]),
            closing=_amount(values[1]),
            total_debit=_amount(values[2]),
            total_credit=_amount(values[3]),
        )
    except IndexError:
        raise BankParseError("statement header is incomplete") from None


def _mandiri_left_tokens(
    words: list[Word],
) -> tuple[list[tuple[float, date]], list[tuple[float, str]]]:
    """Posting-date column: the 'DD Mon YYYY,' triples and the 'HH:MM:SS' times, with their tops."""
    left = sorted((w for w in words if w.x0 < _REMARK_X), key=lambda w: (w.top, w.x0))
    dates: list[tuple[float, date]] = []
    times: list[tuple[float, str]] = []
    i = 0
    while i < len(left):
        w = left[i]
        if _TIME_RE.fullmatch(w.text):
            times.append((w.top, w.text))
        elif (
            i + 2 < len(left)
            and re.fullmatch(r"\d{2}", w.text)
            and re.fullmatch(r"[A-Z][a-z]{2}", left[i + 1].text)
            and re.fullmatch(r"\d{4},?", left[i + 2].text)
        ):
            dates.append(
                (w.top, _month_date(w.text, left[i + 1].text, left[i + 2].text.rstrip(",")))
            )
            i += 2
        i += 1
    return dates, times


def _mandiri_rows(body: list[Word]) -> list[_MandiriRow]:
    anchors: list[tuple[float, Decimal, Decimal, Decimal]] = []
    amounts = [w for w in body if w.x0 >= _AMOUNT_X and _AMOUNT_RE.fullmatch(w.text)]
    for line in _lines(amounts, tol=2.0):
        cols = (
            [w for w in line if w.x1 < _DEBIT_CREDIT_SPLIT],
            [w for w in line if _DEBIT_CREDIT_SPLIT <= w.x1 < _CREDIT_BALANCE_SPLIT],
            [w for w in line if w.x1 >= _CREDIT_BALANCE_SPLIT],
        )
        if any(len(c) != 1 for c in cols):
            raise BankParseError(
                "a statement row does not have one debit, credit and balance amount"
            )
        anchors.append((line[0].top, *(_amount(c[0].text) for c in cols)))
    if not anchors:
        return []

    def nearest(top: float) -> int:
        return min(range(len(anchors)), key=lambda k: abs(anchors[k][0] - top))

    dates, times = _mandiri_left_tokens(body)
    day_of: list[list[date]] = [[] for _ in anchors]
    time_of: list[list[str]] = [[] for _ in anchors]
    for top, d in dates:
        day_of[nearest(top)].append(d)
    for top, t in times:
        time_of[nearest(top)].append(t)
    remark_words: list[list[Word]] = [[] for _ in anchors]
    ref_words: list[list[Word]] = [[] for _ in anchors]
    for w in body:
        if _REMARK_X <= w.x0 < _REF_X:
            remark_words[nearest(w.top)].append(w)
        elif _REF_X <= w.x0 < _AMOUNT_X:
            ref_words[nearest(w.top)].append(w)

    rows: list[_MandiriRow] = []
    for k, (top, debit, credit, balance) in enumerate(anchors):
        if len(day_of[k]) != 1 or len(time_of[k]) != 1:
            raise BankParseError("a statement row does not have exactly one posting date and time")
        if debit > 0 and credit > 0:
            raise BankParseError("row has both a debit and a credit amount")
        h, m, s = (int(x) for x in time_of[k][0].split(":"))
        try:
            posted = datetime(day_of[k][0].year, day_of[k][0].month, day_of[k][0].day, h, m, s)
        except ValueError:
            raise BankParseError("a posting time in the statement is not a real time") from None
        rows.append(
            _MandiriRow(
                top,
                posted,
                debit,
                credit,
                balance,
                _text(ref_words[k]),
                _text(remark_words[k]),
            )
        )
    return rows


def _orient(rows: list, opening: Decimal, closing: Decimal) -> list:
    """File order, or its reverse when only the reverse chains from the printed opening."""

    def breaks(seq: list) -> int:
        n = 0
        prev = opening
        for r in seq:
            if prev + r.credit - r.debit != r.balance:
                n += 1
            prev = r.balance
        return n + (0 if prev == closing else 1)

    return rows if breaks(rows) <= breaks(rows[::-1]) else rows[::-1]


def parse_mandiri_pages(pages: list[list[Word]]) -> ParsedStatement:
    """Bank Mandiri Kopra 'Account Statement' pages -> ParsedStatement (oldest row first)."""
    header: _MandiriHeader | None = None
    rows: list[_MandiriRow] = []
    for words in pages:
        table = next((w.top for w in words if w.text == "Posting" and w.x0 < 60), None)
        if table is None:
            raise BankParseError("statement page has no transaction table")
        page_header = _mandiri_header([w for w in words if w.top < table - 1])
        if header is None:
            header = page_header
        elif page_header != header:
            raise BankParseError("file holds more than one account: upload one statement per file")
        foot = min((w.top for w in words if w.text.startswith("questions")), default=float("inf"))
        rows += _mandiri_rows([w for w in words if table + 6 < w.top < foot - 3])
    if header is None:
        raise BankParseError("statement has no header")
    if header.currency != "IDR":
        raise BankParseError(_NOT_IDR)
    if not rows:
        raise BankParseError(_NO_ROWS)

    rows = _orient(rows, header.opening, header.closing)
    if rows[0].balance - rows[0].credit + rows[0].debit != header.opening:
        raise BankParseError("statement does not start from its printed opening balance")
    if rows[-1].balance != header.closing:
        raise BankParseError("statement does not end at its printed closing balance")
    if (
        sum((r.debit for r in rows), Decimal(0)) != header.total_debit
        or sum((r.credit for r in rows), Decimal(0)) != header.total_credit
    ):
        raise BankParseError("statement rows do not add up to the totals printed in its header")
    txns = [
        _txn(r.posted_at, r.remark, r.debit, r.credit, r.balance, {"Reference No.": r.reference})
        for r in rows
    ]
    return ParsedStatement(
        account_number=header.account,
        account_name=header.name,
        metadata={"Account No.": header.account, "Account Name": header.name, "Currency": "IDR"},
        transactions=txns,
        order_decided_by="balance_chain",
        bank_code="bank_mandiri",
        source_format="pdf",
        kept_columns=_MANDIRI_KEPT,
    )


# ── Bank Permata (Transaction Inquiry) ──────────────────────────────────────

_PERMATA_KEPT = ("Eff Date", "Transaction Code", "Cheque Number", "Ref No", "Customer Ref No")
_PERMATA_DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
_PERMATA_ROW_RE = re.compile(rf"(\d+?)({_PERMATA_DATE})")
_PERMATA_DEBIT_CREDIT_SPLIT = 775.0  # right edge: debit ends near 720, credit near 830


def _permata_date(raw: str) -> date:
    d, mon, y = raw.split("-")
    return _month_date(d, mon, y)


def _permata_zone_amounts(line: list[Word]) -> list[tuple[str, Decimal]]:
    """Amounts in the debit / credit columns of one line, as (column, value)."""
    return [
        ("credit" if w.x1 >= _PERMATA_DEBIT_CREDIT_SPLIT else "debit", _amount(w.text))
        for w in line
        if w.x1 >= 700 and w.x0 >= 600 and _AMOUNT_RE.fullmatch(w.text)
    ]


@dataclass
class _PermataRow:
    post: date
    eff: str
    code: str
    cheque: str
    ref: str
    customer_ref: str
    description: list[str]
    debit: Decimal | None = None
    credit: Decimal | None = None


def _permata_header(words: list[Word]) -> tuple[str, str, str, str]:
    """(account number, account name, currency, period text) from the page head."""
    for line in _lines(words):
        if line[0].text == "Account" and line[0].x0 < 100:
            after = " ".join(w.text for w in line[1:] if w.x0 > 290)
            m = re.fullmatch(r"(\d+)/(.*?)\s*\(\s*([A-Z]{3})\s*\)", after)
            if m:
                return m.group(1), m.group(2).strip(), m.group(3), ""
    raise BankParseError("statement header has no account")


def parse_permata_pages(pages: list[list[Word]]) -> ParsedStatement:
    """Bank Permata 'Transaction Inquiry' pages -> ParsedStatement (oldest row first).

    The report prints no balance per row, only the ledger balance at the start of each day with
    transactions, so each row's balance is that opening plus the day's rows up to it, and the
    next day's printed opening must equal it. A row has a posting DATE, not a time: it is
    stamped at midnight and told apart from identical rows by the import's ordinal."""
    identity: tuple[str, str, str, str] | None = None
    running: Decimal | None = None
    opening: Decimal | None = None
    closing: Decimal | None = None
    done: list[tuple[_PermataRow, Decimal]] = []
    day_debit = day_credit = Decimal(0)
    expect_totals = False
    row: _PermataRow | None = None

    def finish() -> None:
        nonlocal row, running, day_debit, day_credit
        if row is None:
            return
        if row.debit is None or row.credit is None:
            raise BankParseError("a statement row does not have a debit and a credit amount")
        if row.debit > 0 and row.credit > 0:
            raise BankParseError("row has both a debit and a credit amount")
        if running is None:
            raise BankParseError("statement has a row before its opening balance")
        running = running + row.credit - row.debit
        day_debit += row.debit
        day_credit += row.credit
        done.append((row, running))
        row = None

    for words in pages:
        ident = _permata_header([w for w in words if w.top < 300])
        if identity is None:
            identity = ident
        elif ident != identity:
            raise BankParseError("file holds more than one account: upload one statement per file")
        code_top = next((w.top for w in words if w.text == "Code" and 200 < w.x0 < 270), None)
        if code_top is None:
            raise BankParseError("statement page has no transaction table")
        stop = False
        for line in _lines([w for w in words if w.top > code_top + 3]):
            first = line[0]
            names = [w.text for w in line]
            amounts_in_line = _permata_zone_amounts(line)
            if first.x0 < 60 and first.text in ("Opening", "Ledger") and "Ledger" in names:
                finish()
                value = [w for w in line if _AMOUNT_RE.fullmatch(w.text) and w.x0 > 600]
                when = [w for w in line if re.fullmatch(_PERMATA_DATE, w.text)]
                if len(value) != 1 or len(when) != 1:
                    raise BankParseError("an opening balance line in the statement is unreadable")
                balance = _amount(value[0].text)
                if running is None:
                    opening = running = balance
                elif running != balance:
                    raise BankParseError("a day's opening balance does not follow the previous day")
                day_debit = day_credit = Decimal(0)
                expect_totals = False
            elif first.x0 < 60 and first.text == "Total":
                finish()
                expect_totals = True
            elif first.x0 < 60 and first.text == "Closing" and "Ledger" in names:
                finish()
                value = [w for w in line if _AMOUNT_RE.fullmatch(w.text) and w.x0 > 600]
                if len(value) != 1:
                    raise BankParseError("the closing balance line in the statement is unreadable")
                closing = _amount(value[0].text)
                stop = True
            elif expect_totals and amounts_in_line:
                got = dict(amounts_in_line)
                if (
                    len(amounts_in_line) != 2
                    or got.get("debit") != day_debit
                    or got.get("credit") != day_credit
                ):
                    raise BankParseError(
                        "a day's rows do not add up to the total printed under them"
                    )
                expect_totals = False
            else:
                left = "".join(w.text for w in line if w.x0 < 110)
                m = _PERMATA_ROW_RE.fullmatch(left) if left else None
                if m:
                    finish()

                    def cell(lo: float, hi: float, line: list[Word] = line) -> str:
                        return " ".join(w.text for w in line if lo <= w.x0 < hi)

                    row = _PermataRow(
                        post=_permata_date(m.group(2)),
                        eff=cell(110, 200),
                        code=cell(200, 265),
                        cheque=cell(265, 345),
                        ref=cell(345, 445),
                        customer_ref=cell(445, 500),
                        description=[],
                    )
                if row is not None:
                    cells = [w.text for w in line if w.x0 >= 500 and w.x1 < 650]
                    if cells:
                        row.description.append(" ".join(cells))
                    for column, value_ in amounts_in_line:
                        if column == "debit" and row.debit is None:
                            row.debit = value_
                        elif column == "credit" and row.credit is None:
                            row.credit = value_
            if stop:
                break
    if identity is None or closing is None or opening is None:
        raise BankParseError("statement is incomplete: no opening or closing balance line")
    finish()
    if identity[2] != "IDR":
        raise BankParseError(_NOT_IDR)
    if not done:
        raise BankParseError(_NO_ROWS)
    if running != closing:
        raise BankParseError("statement does not end at its printed closing balance")
    txns = [
        _txn(
            datetime(r.post.year, r.post.month, r.post.day),
            " ".join(r.description),
            r.debit or Decimal(0),
            r.credit or Decimal(0),
            balance,
            {
                "Eff Date": r.eff,
                "Transaction Code": r.code,
                "Cheque Number": r.cheque,
                "Ref No": r.ref,
                "Customer Ref No": r.customer_ref,
            },
        )
        for r, balance in done
    ]
    return ParsedStatement(
        account_number=identity[0],
        account_name=identity[1],
        metadata={"Account No.": identity[0], "Account Name": identity[1], "Currency": "IDR"},
        transactions=txns,
        order_decided_by="balance_chain",
        bank_code="bank_permata",
        source_format="pdf",
        kept_columns=_PERMATA_KEPT,
    )


# ── the one entry point of the upload ───────────────────────────────────────

_UNKNOWN = "file is not a bank statement this page reads (CIMB Niaga CSV, Bank Mandiri PDF, Bank Permata PDF)"


def _parse(data: bytes) -> ParsedStatement:
    if data.startswith(b"%PDF-"):
        pages = extract_pdf_pages(data)
        kind = detect_pdf_format(pages)
        if kind == "mandiri":
            return parse_mandiri_pages(pages)
        if kind == "permata":
            return parse_permata_pages(pages)
        raise BankParseError(_UNKNOWN)
    try:
        text = data.decode(
            "utf-8"
        )  # one BOM is left in the text, the CIMB parser removes exactly one
    except UnicodeDecodeError:
        raise BankParseError("file is not valid UTF-8 text") from None
    return parse_cimb_statement(text)


def parse_and_preview_statement(data: bytes) -> tuple[ParsedStatement, dict[str, object]]:
    """Detect the format from the bytes, parse, and summarise: total (BankParseError or a result)."""

    def run() -> tuple[ParsedStatement, dict[str, object]]:
        stmt = _parse(data)
        return stmt, build_preview_report(stmt)

    return _total(run)


def preview_statement(data: bytes) -> dict[str, object]:
    return parse_and_preview_statement(data)[1]


__all__ = [
    "Word",
    "detect_pdf_format",
    "extract_pdf_pages",
    "parse_and_preview_statement",
    "parse_mandiri_pages",
    "parse_permata_pages",
    "preview_statement",
]
