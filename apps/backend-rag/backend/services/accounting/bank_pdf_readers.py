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
    if "Account Information" in text and (
        "TRANSACTION INQUIRY" in text or "ACCOUNT STATEMENT" in text
    ):
        return "bni"
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


# Kopra leaves clear space between two rows: measured on real statements the lines of one row
# sit at most ~10 pt apart and the next row starts 30 pt or more below the last line of the
# previous one. A new block therefore starts at a vertical gap over _BLOCK_GAP.
_BLOCK_GAP = 20.0


def _mandiri_blocks(body: list[Word]) -> list[list[Word]]:
    """The body words grouped into row blocks by vertical gaps (never by proximity to an anchor)."""
    blocks: list[list[Word]] = []
    last_top = None
    for line in _lines(body, tol=2.0):
        if last_top is None or line[0].top - last_top > _BLOCK_GAP:
            blocks.append([])
        blocks[-1].extend(line)
        last_top = max(w.top for w in line)
    return blocks


def _mandiri_rows(body: list[Word]) -> list[_MandiriRow]:
    rows: list[_MandiriRow] = []
    for block in _mandiri_blocks(body):
        amount_lines = [
            line
            for line in _lines([w for w in block if w.x0 >= _AMOUNT_X], tol=2.0)
            if any(_AMOUNT_RE.fullmatch(w.text) for w in line)
        ]
        stray = [w for w in block if w.x0 >= _AMOUNT_X and not _AMOUNT_RE.fullmatch(w.text)]
        if len(amount_lines) != 1 or stray:
            raise BankParseError("a statement row block does not have exactly one amount line")
        line = amount_lines[0]
        cols = (
            [w for w in line if w.x1 < _DEBIT_CREDIT_SPLIT],
            [w for w in line if _DEBIT_CREDIT_SPLIT <= w.x1 < _CREDIT_BALANCE_SPLIT],
            [w for w in line if w.x1 >= _CREDIT_BALANCE_SPLIT],
        )
        if any(len(c) != 1 for c in cols):
            raise BankParseError(
                "a statement row does not have one debit, credit and balance amount"
            )
        debit, credit, balance = (_amount(c[0].text) for c in cols)
        dates, times = _mandiri_left_tokens(block)
        left = [w for w in block if w.x0 < _REMARK_X]
        if len(dates) != 1 or len(times) != 1 or len(left) != 4:
            raise BankParseError(
                "a statement row block does not have exactly one posting date and time"
            )
        if debit > 0 and credit > 0:
            raise BankParseError("row has both a debit and a credit amount")
        h, m, s = (int(x) for x in times[0][1].split(":"))
        day = dates[0][1]
        try:
            posted = datetime(day.year, day.month, day.day, h, m, s)
        except ValueError:
            raise BankParseError("a posting time in the statement is not a real time") from None
        remark = [w for w in block if _REMARK_X <= w.x0 < _REF_X]
        ref = [w for w in block if _REF_X <= w.x0 < _AMOUNT_X]
        rows.append(
            _MandiriRow(line[0].top, posted, debit, credit, balance, _text(ref), _text(remark))
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
        foot = min((w.top for w in words if "questions" in w.text), default=float("inf"))
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
        balances_derived=True,
        kept_columns=_PERMATA_KEPT,
    )


# ── BNI (BNIdirect "Transaction Inquiry" and "Account Statement") ───────────

_BNI_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_BNI_TIME_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}")
_BNI_ACCOUNT_RE = re.compile(r"(\d{6,})\s*/\s*(.*?)\s*\(\s*([A-Za-z]*)\s*\)")
_BNI_AMT = r"(-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})"
_BNI_CREDIT, _BNI_DEBIT = ("C", "K"), ("D",)


@dataclass
class _BniRow:
    top: float
    posted_at: datetime
    debit: Decimal
    credit: Decimal
    balance: Decimal | None
    journal: str
    description: str


def _bni_find(text: str, pattern: str) -> re.Match[str] | None:
    return re.search(pattern, text)


_BNI_HEADER_WORDS = frozenset(
    ("Post", "Posting", "Date", "Effective", "Branch", "Journal", "No.", "Transaction",
     "Description", "Amount", "Db/Cr", "DB/C", "DB/CR", "R", "Balance")
)  # fmt: skip
_BNI_CHROME_WORDS = frozenset(
    ("Account", "Period", "Beginning", "Total", "Information", "TRANSACTION", "INQUIRY",
     "ACCOUNT", "STATEMENT", "Page", "Ending", "Type")
)  # fmt: skip


def _bni_cells(
    words: list[Word],
    flags: list[Word],
    cols: tuple[Word, Word, Word | None],
    no_h: Word | None,
    top0: float,
) -> tuple[list[list[Word]], list[list[Word]], list[list[Word]], list[list[Word]]]:
    """Dates, times, description words and journal words of each row (by flag), with every word
    placed by the layout and none by proximity. A print with a row-number column (Transaction
    Inquiry) cuts rows at the numbers; one without (Account Statement) centres every cell on the flag
    line, so the description lines are cut into runs centred on each flag (one cut or refusal)
    and the other cells are taken from the flag's own line. A row that
    does not come out with exactly one flag, date and time, text that fits no row, or any other
    count, refuses the file."""
    _, amt_h, desc_h = cols
    header_tops = [w.top for w in words if w.text.lower() in ("db/cr", "db/c")]
    # page chrome: the title, address and account lines (and the footer) repeat, anywhere on a page
    tops = sorted(w.top for w in words if w.text in _BNI_CHROME_WORDS and w.x0 < 330)
    bands: list[tuple[float, float]] = []
    for t in tops:
        if bands and t - bands[-1][1] <= 30:
            bands[-1] = (bands[-1][0], t)
        else:
            bands.append((t, t))
    bands = [(lo - 6, hi + 12) for lo, hi in bands]
    body = [
        w
        for w in words
        if w.top > top0
        and not any(lo <= w.top <= hi for lo, hi in bands)
        and not (w.text in _BNI_HEADER_WORDS and any(-10 <= w.top - t <= 14 for t in header_tops))
    ]
    n = len(flags)
    dates: list[list[Word]] = [[] for _ in flags]
    times: list[list[Word]] = [[] for _ in flags]
    desc: list[list[Word]] = [[] for _ in flags]
    journal: list[list[Word]] = [[] for _ in flags]

    def in_desc(w: Word) -> bool:
        return desc_h.x1 - 1 <= w.x0 < amt_h.x0 - 2 and not _AMOUNT_RE.fullmatch(w.text)

    def place(k: int, w: Word) -> None:
        if _BNI_DATE_RE.fullmatch(w.text):
            dates[k].append(w)
        elif _BNI_TIME_RE.fullmatch(w.text):
            times[k].append(w)
        elif in_desc(w):
            desc[k].append(w)
        elif _bni_in_journal_column(cols, w) and w.text.isdigit():
            journal[k].append(w)

    unplaced = "a statement row block cannot be told apart: text does not fit one row"
    if no_h is not None:
        marks = sorted(
            w.top
            for w in body
            if w.text.isdigit() and no_h.x0 - 8 <= w.x0 <= no_h.x0 + 12 and w.x1 < desc_h.x0 - 40
        )
        if len(marks) != n:
            raise BankParseError(unplaced)
        edges = [m - 3 for m in marks] + [float("inf")]
        for k, f in enumerate(flags):
            if not edges[k] <= f.top < edges[k + 1]:
                raise BankParseError(unplaced)
        for w in body:
            if w.top < edges[0]:
                if _BNI_DATE_RE.fullmatch(w.text) or _BNI_TIME_RE.fullmatch(w.text) or in_desc(w):
                    raise BankParseError(unplaced)
                continue
            k = max(j for j in range(n) if edges[j] <= w.top)
            place(k, w)
        for k in range(n):
            if len(dates[k]) != 1 or len(times[k]) != 1:
                raise BankParseError(unplaced)
        return dates, times, desc, journal

    # No row number and no vertical gap tells where one description ends: the cells of a row are
    # vertically centred on its flag line. Cut the description lines into consecutive runs, one
    # per flag, each centred on its flag (a page break may drop a chrome band into the middle of a
    # row: the lines after the band carry on the run above it). Exactly one cut may fit, else refuse.
    lines = _lines([w for w in body if in_desc(w)], tol=2.0)
    ltops = [line[0].top for line in lines]

    carry = [False]  # whether a run may cross a chrome band (tried only when none fits otherwise)

    def fits(i: int, j: int, f: Word) -> bool:  # lines[i:j] as the description of flag f
        if i >= j:
            return False
        run = ltops[i:j]
        for lo, hi in bands:  # lines after a band continue the row above it
            if run[0] < lo and run[-1] > hi:
                if not carry[0] or run[-1] - hi > 60:
                    return False
                run = [t for t in run if t < lo]
        return abs((run[0] + run[-1]) / 2 - f.top) <= 4 and run[0] - 3 <= f.top <= run[-1] + 3

    memo: dict[tuple[int, int], list[list[int]]] = {}

    def cuts(i: int, k: int) -> list[list[int]]:  # up to two ways to place lines[i:] on flags[k:]
        if (i, k) in memo:
            return memo[(i, k)]
        out: list[list[int]] = []
        if k == n:
            out = [[]] if i == len(lines) else []
        else:
            for j in range(i + 1, len(lines) + 1):
                if fits(i, j, flags[k]):
                    out += [[j, *rest] for rest in cuts(j, k + 1)]
                    if len(out) > 1:
                        break
        memo[(i, k)] = out[:2]
        return out[:2]

    found = cuts(0, 0)
    if not found:
        carry[0] = True
        memo.clear()
        found = cuts(0, 0)
    if len(found) != 1:
        raise BankParseError(unplaced)
    start = 0
    for k, end in enumerate(found[0]):
        desc[k] = [w for line in lines[start:end] for w in line]
        start = end
    for w in body:
        if in_desc(w):
            continue
        owners = [k for k, f in enumerate(flags) if abs(w.top - f.top) <= 3]
        if len(owners) == 1:
            place(owners[0], w)
    for k in range(n):
        if not 1 <= len(dates[k]) <= 2 or not 1 <= len(times[k]) <= 2:
            raise BankParseError(unplaced)
    return dates, times, desc, journal


def parse_bni_pages(pages: list[list[Word]]) -> ParsedStatement:
    """BNI 'Transaction Inquiry' / 'Account Statement' pages -> ParsedStatement (file order).

    The page scale differs between prints, so the columns are read from the header row. A row is
    anchored on its Db/Cr flag (C or K credit, D debit); its amount is the amount left of the flag,
    its balance the amount right of it. The 'Transaction Inquiry' header prints the beginning
    balance and both totals; the 'Account Statement' prints the beginning balance as a lone
    balance before the first row and, at the end, the ending balance and totals with row counts
    (its last row carries no balance of its own: it must equal the ending balance)."""
    cols: tuple[Word, Word, Word | None] | None = None  # flag header, amount header, journal header
    account: tuple[str, str, str] | None = None
    text_all = " ".join(" ".join(w.text for w in _lines_flat(p)) for p in pages)
    begin_m = _bni_find(text_all, rf"Beginning Balance\s*:\s*{_BNI_AMT}")
    tdeb_m = _bni_find(text_all, rf"Total Deb(?:it|et)\s*:\s*(\d+\s+)?{_BNI_AMT}")
    tcred_m = _bni_find(text_all, rf"Total Credit\s*:\s*(\d+\s+)?{_BNI_AMT}")
    end_m = _bni_find(text_all, rf"Ending Balance\s*:\s*{_BNI_AMT}")
    rows: list[_BniRow] = []
    lone: Decimal | None = None
    no_h: Word | None = None  # the row-number header: only some prints have one
    for page_no, words in enumerate(pages):
        flag_h = next((w for w in words if w.text.lower() in ("db/cr", "db/c")), None)
        if flag_h is not None:
            amt_h = next(
                (w for w in words if w.text == "Amount" and abs(w.top - flag_h.top) < 12), None
            )
            jr_h = next(
                (w for w in words if w.text == "Journal" and abs(w.top - flag_h.top) < 12), None
            )
            if amt_h is None or jr_h is None:
                raise BankParseError("statement table header is incomplete")
            nxt = next(
                (
                    w
                    for w in words
                    if w.text == "No." and 0 <= w.x0 - jr_h.x1 < 6 and abs(w.top - jr_h.top) < 3
                ),
                None,
            )
            cols = (flag_h, amt_h, nxt or jr_h)
            no_h = next(
                (
                    w
                    for w in words
                    if w.text == "No." and w.x0 < 120 and abs(w.top - flag_h.top) < 12
                ),
                None,
            )
        if cols is None:
            if page_no == 0:
                raise BankParseError("statement page has no transaction table")
            continue
        flag_h, amt_h, _ = cols
        # the header row repeats anywhere on later pages: only page 1's marks where rows start
        top0 = flag_h.top + 2 if page_no == 0 else -1.0
        if account is None:
            for line in _lines(words):
                m = _BNI_ACCOUNT_RE.search(" ".join(w.text for w in line))
                if m and re.search(r"Account", " ".join(w.text for w in line)):
                    account = (m.group(1), m.group(2).strip(), m.group(3).upper())
                    break
        flags = sorted(
            (
                w
                for w in words
                if w.text in _BNI_CREDIT + _BNI_DEBIT
                and w.top > top0
                and flag_h.x0 - 6 <= w.x0 <= flag_h.x1 + 6
            ),
            key=lambda w: w.top,
        )
        amounts = [w for w in words if _AMOUNT_RE.fullmatch(w.text) and w.top > top0]
        if page_no == 0 and (begin_m is None):  # 'Account Statement': a lone balance before row 1
            first = flags[0].top if flags else float("inf")
            loose = [
                w
                for w in amounts
                if w.x0 >= flag_h.x1
                and w.top < first - 3
                and not any(abs(w.top - f.top) <= 3 for f in flags)
            ]
            if len(loose) == 1:
                lone = _amount(loose[0].text)
        if not flags:
            continue

        dates, times, desc, journal = _bni_cells(words, flags, cols, no_h, top0)
        for k, f in enumerate(flags):
            if not dates[k] or not times[k]:
                raise BankParseError("a statement row does not have a posting date and time")
            d = min(dates[k], key=lambda w: w.x0).text
            t = min(times[k], key=lambda w: w.x0).text
            try:
                posted = datetime(
                    int(d[6:]), int(d[3:5]), int(d[:2]), int(t[:2]), int(t[3:5]), int(t[6:])
                )
            except ValueError:
                raise BankParseError(
                    "a posting date or time in the statement is not real"
                ) from None
            near = [w for w in amounts if abs(w.top - f.top) <= 3]
            left = [w for w in near if w.x1 <= f.x0 + 1]
            right = [w for w in near if w.x0 >= f.x1 - 1]
            if len(left) != 1 or len(right) > 1:
                raise BankParseError("a statement row does not have one amount and one balance")
            value = _amount(left[0].text)
            if value < 0:
                raise BankParseError("a statement row has a negative amount")
            is_credit = f.text in _BNI_CREDIT
            rows.append(
                _BniRow(
                    f.top,
                    posted,
                    Decimal(0) if is_credit else value,
                    value if is_credit else Decimal(0),
                    _amount(right[0].text) if right else None,
                    " ".join(w.text for w in journal[k]),
                    " ".join(
                        w.text for w in sorted(desc[k], key=lambda w: (round(w.top / 3), w.x0))
                    ),
                )
            )
    if account is None or not rows:
        raise BankParseError(_NO_ROWS if account else "statement header has no account")
    if account[2] not in ("IDR", ""):
        raise BankParseError(_NOT_IDR)
    beginning = _amount(begin_m.group(1)) if begin_m else lone
    if beginning is None:
        raise BankParseError("statement has no beginning balance")
    if tdeb_m is None or tcred_m is None:
        raise BankParseError("statement has no printed totals")
    total_debit, total_credit = _amount(tdeb_m.group(2)), _amount(tcred_m.group(2))
    if (
        sum((r.debit for r in rows), Decimal(0)) != total_debit
        or sum((r.credit for r in rows), Decimal(0)) != total_credit
    ):
        raise BankParseError("statement rows do not add up to the totals printed in it")
    for m, side in ((tdeb_m, "debit"), (tcred_m, "credit")):
        if m.group(1) and int(m.group(1)) != sum(1 for r in rows if getattr(r, side) > 0):
            raise BankParseError("statement row count does not match the count printed in it")
    closing = beginning + total_credit - total_debit
    if end_m is not None and _amount(end_m.group(1)) != closing:
        raise BankParseError("statement does not end at its printed ending balance")
    if rows[-1].balance is None and end_m is not None:
        before = rows[-2].balance if len(rows) > 1 else beginning
        if before is not None and before + rows[-1].credit - rows[-1].debit == closing:
            rows[-1].balance = closing
    if any(r.balance is None for r in rows):
        raise BankParseError("a statement row has no balance")
    rows = _orient(rows, beginning, closing)
    if rows[0].balance - rows[0].credit + rows[0].debit != beginning:  # type: ignore[operator]
        raise BankParseError("statement does not start from its beginning balance")
    if rows[-1].balance != closing:
        raise BankParseError(
            "statement does not end at beginning balance plus credits minus debits"
        )
    txns = [
        _txn(r.posted_at, r.description, r.debit, r.credit, r.balance, {"Journal No.": r.journal})  # type: ignore[arg-type]
        for r in rows
    ]
    return ParsedStatement(
        account_number=account[0],
        account_name=account[1],
        metadata={"Account No.": account[0], "Account Name": account[1], "Currency": "IDR"},
        transactions=txns,
        order_decided_by="balance_chain",
        bank_code="bni",
        source_format="pdf",
        kept_columns=("Journal No.",),
    )


def _lines_flat(words: list[Word]) -> list[Word]:
    return [w for line in _lines(words) for w in line]


def _bni_in_journal_column(cols: tuple[Word, Word, Word | None], w: Word) -> bool:
    """A word in the Journal No. column (between the branch text and the description)."""
    desc_h = cols[2]
    return desc_h is not None and desc_h.x0 - 40 <= w.x0 < desc_h.x1 + 1


# ── the one entry point of the upload ───────────────────────────────────────

_UNKNOWN = "file is not a bank statement this page reads (CIMB Niaga CSV, Bank Mandiri, BNI or Bank Permata PDF)"


def _parse(data: bytes) -> ParsedStatement:
    if data.startswith(b"%PDF-"):
        pages = extract_pdf_pages(data)
        kind = detect_pdf_format(pages)
        if kind == "mandiri":
            return parse_mandiri_pages(pages)
        if kind == "permata":
            return parse_permata_pages(pages)
        if kind == "bni":
            return parse_bni_pages(pages)
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
