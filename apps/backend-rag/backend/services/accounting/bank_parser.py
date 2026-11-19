"""Bank statement CSV parser — per-bank config adapter (Monopoly-pattern).

No maintained library parses Indonesian-bank exports, so this is a thin,
config-driven normalizer: each bank has a BankConfig describing its column
layout, and parse_csv() emits a canonical list of ParsedTxn. PII (payer names)
stays local — this runs on the box, never sends rows to a cloud LLM.

Indonesian gotchas handled:
  - dd/mm/yyyy (and dd/mm with no year) dates
  - decimal-comma / thousands-dot amounts ("1.234.567,89") OR plain ("1234567")
  - separate Debit/Credit columns OR one signed amount OR amount + DB/CR flag
  - leading-apostrophe text guards ("'04/09)

CIMB Niaga (OCTO Biz "Account Statement Summary" CSV) has its own dedicated
parser below, parse_cimb_statement(): the export is a `Key,Value` metadata
block followed by a transaction table, newest row first, which the generic
header-first BankConfig layout cannot express.
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class ParsedTxn:
    txn_date: date
    description: str
    amount_idr: int
    direction: str  # 'credit' (money in) | 'debit' (money out)
    running_balance_idr: int | None = None
    raw_row: dict[str, str] = field(default_factory=dict)
    # Filled by the CIMB statement parser only (exact Decimal amounts, time of day, kind).
    posted_at: datetime | None = None
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    balance: Decimal | None = None
    kind: str = "other"


@dataclass
class BankConfig:
    """Describes how to read one bank's CSV layout."""

    bank_code: str
    date_col: str
    desc_col: str
    date_formats: tuple[str, ...] = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y")
    # amount handling — exactly one of these modes:
    debit_col: str | None = None      # two-column mode: debit / credit
    credit_col: str | None = None
    amount_col: str | None = None     # single-amount mode
    flag_col: str | None = None       # amount + DB/CR flag mode
    credit_flag_values: tuple[str, ...] = ("CR", "C", "K", "KREDIT", "CREDIT")
    balance_col: str | None = None
    decimal_comma: bool = False       # True => "1.234.567,89"; False => "1,234,567.89"/plain
    delimiter: str = ","              # decimal-comma exports must use ';' (else comma collides)


# A generic single-signed-amount layout (many CSV exports).
GENERIC_SIGNED = BankConfig(
    bank_code="generic",
    date_col="date",
    desc_col="description",
    amount_col="amount",
    balance_col="balance",
    decimal_comma=False,
)

_CONFIGS: dict[str, BankConfig] = {
    "generic": GENERIC_SIGNED,
}


class BankParseError(Exception):
    pass


def _clean_cell(v: str | None) -> str:
    if v is None:
        return ""
    return v.strip().lstrip("'").strip()


def _parse_amount(raw: str, decimal_comma: bool) -> int:
    """Parse an IDR amount string to integer rupiah. Empty -> 0."""
    s = _clean_cell(raw)
    if not s:
        return 0
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()").replace(" ", "")
    if decimal_comma:
        # "1.234.567,89" -> dots are thousands, comma is decimal
        s = s.replace(".", "").replace(",", ".")
    else:
        # "1,234,567.89" -> commas are thousands
        s = s.replace(",", "")
    try:
        val = int(round(float(s)))
    except ValueError as exc:
        raise BankParseError(f"cannot parse amount {raw!r}") from exc
    return -val if neg else val


def _parse_date(raw: str, formats: tuple[str, ...]) -> date:
    s = _clean_cell(raw)
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise BankParseError(f"cannot parse date {raw!r} with {formats}")


def _row_direction_amount(row: dict[str, str], cfg: BankConfig) -> tuple[str, int]:
    """Return (direction, positive amount_idr) for one row, per the config mode."""
    if cfg.debit_col is not None and cfg.credit_col is not None:
        debit = _parse_amount(row.get(cfg.debit_col, ""), cfg.decimal_comma)
        credit = _parse_amount(row.get(cfg.credit_col, ""), cfg.decimal_comma)
        if credit > 0:
            return "credit", credit
        return "debit", abs(debit)
    if cfg.amount_col is not None and cfg.flag_col is not None:
        amt = abs(_parse_amount(row.get(cfg.amount_col, ""), cfg.decimal_comma))
        flag = _clean_cell(row.get(cfg.flag_col, "")).upper()
        direction = "credit" if flag in cfg.credit_flag_values else "debit"
        return direction, amt
    if cfg.amount_col is not None:
        amt = _parse_amount(row.get(cfg.amount_col, ""), cfg.decimal_comma)
        return ("credit" if amt >= 0 else "debit"), abs(amt)
    raise BankParseError("BankConfig has no usable amount mode")


def parse_csv(content: str, bank_code: str = "cimb_niaga") -> list[ParsedTxn]:
    """Parse a bank statement CSV into canonical transactions.

    `content` is the decoded file text. Rows that can't be parsed (header noise,
    totals) are skipped with a warning, never silently dropped from the count.
    """
    if bank_code == "cimb_niaga":
        return parse_cimb_statement(content).transactions
    cfg = _CONFIGS.get(bank_code)
    if cfg is None:
        raise BankParseError(f"unknown bank_code {bank_code!r}; known: {list(_CONFIGS)}")

    reader = csv.DictReader(io.StringIO(content), delimiter=cfg.delimiter)
    if reader.fieldnames is None:
        raise BankParseError("empty CSV / no header row")

    out: list[ParsedTxn] = []
    skipped = 0
    for row in reader:
        # need a date and an amount-bearing cell to be a real transaction row
        if not _clean_cell(row.get(cfg.date_col, "")):
            skipped += 1
            continue
        try:
            txn_date = _parse_date(row[cfg.date_col], cfg.date_formats)
            direction, amount = _row_direction_amount(row, cfg)
            if amount == 0:
                skipped += 1
                continue
            balance = (
                _parse_amount(row.get(cfg.balance_col, ""), cfg.decimal_comma)
                if cfg.balance_col
                else None
            )
            out.append(
                ParsedTxn(
                    txn_date=txn_date,
                    description=_clean_cell(row.get(cfg.desc_col, "")),
                    amount_idr=amount,
                    direction=direction,
                    running_balance_idr=balance,
                    raw_row={k: _clean_cell(v) for k, v in row.items()},
                )
            )
        except BankParseError as exc:
            logger.warning("bank_parser: skipping unparseable row: %s (%s)", row, exc)
            skipped += 1

    if skipped:
        logger.info("bank_parser[%s]: parsed %d txns, skipped %d rows", bank_code, len(out), skipped)

    # D7 (superscar #2: no green-but-empty): if the file clearly had data rows
    # but we parsed NOTHING, that's almost certainly a wrong delimiter / wrong
    # bank_code / unexpected layout — fail loudly instead of returning [] silently.
    if not out and skipped > 0:
        raise BankParseError(
            f"parsed 0 transactions from {skipped} non-empty row(s) for bank_code={bank_code!r} "
            f"(delimiter={cfg.delimiter!r}) — likely wrong delimiter, bank_code, or column layout"
        )
    return out


def verify_balance(
    txns: list[ParsedTxn], opening_balance_idr: int, closing_balance_idr: int
) -> bool:
    """Golden-rule check: opening + sum(credits) - sum(debits) == closing.

    Returns True if the running total reconciles (within 1 rupiah). The caller
    surfaces a failure loudly (superscar #2) instead of trusting a bad parse.
    """
    delta = sum(t.amount_idr if t.direction == "credit" else -t.amount_idr for t in txns)
    return abs((opening_balance_idr + delta) - closing_balance_idr) <= 1


# ── CIMB Niaga OCTO Biz "Account Statement Summary" ──────────────────────────

# The export's header exactly as the bank writes it (spec S1). Any other layout is refused:
# a changed export is a reason to stop and look, not to guess.
_CIMB_HEADER = (
    "Post Date", "Payment Date", "Payment Type", "Other Reference", "Bank Reference",
    "Debit Amount", "Credit Amount", "Balance", "Description", "Cheque Number",
)
_CIMB_HEADER_FIRST_CELL = _CIMB_HEADER[0]
_MAX_STATEMENT_LINES = 20_000  # spec S1/S8: more refuses the file before any csv parsing
_HEADER_SEARCH_LINES = 64  # spec S1: the header must sit within the first 64 lines


def _trim(value: str) -> str:
    """Spec S1: 'trimmed' is spaces and tabs only; no other Unicode whitespace, no apostrophe."""
    return value.strip(" \t")


def _is_blank(cells: list[str]) -> bool:
    """A row is blank when every cell is empty after trimming, however many cells it has."""
    return all(not _trim(c) for c in cells)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}

KINDS = (
    "client_credit", "government_fee", "transfer_out", "transfer_fee",
    "cash_withdrawal", "bank_fee", "payroll", "tax", "interest", "other",
)

# Exact bank payment types. Credits into the account are matched by prefix below.
_KIND_BY_PAYMENT_TYPE = {
    "TR TO REMITT": "transfer_out",
    "REMITTANCE COMMISSION": "transfer_fee",
    "CHQ CASH WITHDRAWAL": "cash_withdrawal",
    "CHEQUE STAMP DUTY": "bank_fee",
    "MISCELLANEOUS CHARGES": "bank_fee",
    "MONTHLY ADMIN FEE": "bank_fee",
    "DIRECT DEBIT": "payroll",
    "WITHHOLDING TAX": "tax",
    "CREDIT INTEREST": "interest",
}
_CLIENT_CREDIT_PREFIX = "REMITTANCE CR - "  # any channel
_CLIENT_CREDIT_EXACT = frozenset({"OVERBOOKING CR", "OVERBOOKING FROM CA"})


@dataclass
class ParsedStatement:
    account_number: str
    account_name: str
    metadata: dict[str, str]
    transactions: list[ParsedTxn]  # oldest first
    order_decided_by: str = "default"  # 'timestamps' | 'balance_chain' | 'default'
    # What the importer stores about the file: which bank, which format, and which of the row's
    # raw cells it keeps (the other readers live in bank_pdf_readers.py).
    bank_code: str = "cimb_niaga"
    source_format: str = "csv"
    # True when the format prints no balance per row and the reader derived each one from the
    # day's printed opening balance (Permata): a row's balance then depends on the rows before it.
    balances_derived: bool = False
    kept_columns: tuple[str, ...] = (
        "Payment Date", "Payment Type", "Other Reference", "Bank Reference", "Cheque Number",
    )

    @property
    def opening_balance(self) -> Decimal:
        """Balance before the oldest row (the export has no opening-balance line)."""
        first = self.transactions[0]
        assert first.balance is not None
        return first.balance - first.credit + first.debit

    @property
    def closing_balance(self) -> Decimal:
        last = self.transactions[-1].balance
        assert last is not None
        return last


_CREDIT_ONLY_KINDS = frozenset({"client_credit", "interest"})
_DEBIT_ONLY_KINDS = frozenset(
    {"government_fee", "transfer_out", "transfer_fee", "cash_withdrawal", "bank_fee", "payroll", "tax"}
)


def classify_cimb_kind(payment_type: str, description: str, direction: str | None = None) -> str:
    """Row kind from the bank's own Payment Type / Description; unknown -> 'other'.

    When `direction` ('credit' | 'debit') is given, a kind that contradicts where the
    money went (a debit typed as a client credit, a credit typed as a transfer out)
    is 'other'.
    """
    # Exact after trimming: the bank writes its types upper-case with single spaces. No case
    # folding (Unicode upper-casing maps look-alikes onto a known type), no space collapsing.
    ptype = _trim(payment_type or "")
    if ptype == "BILL PAYMENT" and _trim(description or "").startswith("EMPN BILLING"):
        kind = "government_fee"
    elif ptype.startswith(_CLIENT_CREDIT_PREFIX) or ptype in _CLIENT_CREDIT_EXACT:
        kind = "client_credit"
    else:
        kind = _KIND_BY_PAYMENT_TYPE.get(ptype, "other")
    if direction == "debit" and kind in _CREDIT_ONLY_KINDS:
        return "other"
    if direction == "credit" and kind in _DEBIT_ONLY_KINDS:
        return "other"
    return kind


_TIMESTAMP_RE = re.compile(r"[0-9]{2} [A-Z][a-z]{2} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def _parse_cimb_timestamp(raw: str) -> datetime:
    """'17 Nov 2026 15:31:06' (trimmed cell, exact pattern checked BEFORE any conversion,
    English month table, real calendar date and time)."""
    s = _trim(raw)
    if not _TIMESTAMP_RE.fullmatch(s):
        # fixed text: nothing read from the file may reach an error (logged in production)
        raise BankParseError("Post Date is not a timestamp")
    try:
        return datetime(
            int(s[7:11]), _MONTHS[s[3:6].lower()], int(s[0:2]),
            int(s[12:14]), int(s[15:17]), int(s[18:20]),
        )
    except (KeyError, ValueError):
        raise BankParseError("Post Date is not a timestamp") from None


# No leading zeros: an unquoted thousands comma ("1,000.00") splits into "1" and "000.00".
_AMOUNT_RE = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?")
_SIGNED_AMOUNT_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?")


def _parse_decimal(raw: str, what: str, allow_minus: bool = False) -> Decimal:
    """Strict plain decimal; the pattern is checked BEFORE any numeric conversion."""
    s = _trim(raw)  # trim only: no apostrophe or other character is stripped
    pattern = _SIGNED_AMOUNT_RE if allow_minus else _AMOUNT_RE
    if not pattern.fullmatch(s):
        raise BankParseError(f"{what} is not a plain decimal amount")
    return Decimal(s)


def _round_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


# The bank stamps a batch of postings within the same second or one second out of order
# (measured: inversions of exactly 1 s). Two adjacent rows this close carry no date evidence.
TIMESTAMP_JITTER = timedelta(seconds=2)


def _orient_oldest_first(txns: list[ParsedTxn]) -> tuple[list[ParsedTxn], str]:
    """Return (rows oldest-first, what decided). Timestamps decide by majority of adjacent
    pairs further apart than the jitter window (descending D vs ascending A); the balance
    chain is consulted only when D == A and never overrides the dates; if it is tied too,
    reverse (the export is newest-first)."""
    descending = ascending = 0
    for prev, cur in itertools.pairwise(txns):
        gap = prev.posted_at - cur.posted_at  # type: ignore[operator]
        if gap > TIMESTAMP_JITTER:
            descending += 1
        elif -gap > TIMESTAMP_JITTER:
            ascending += 1
    if descending != ascending:
        return (txns[::-1] if descending > ascending else txns), "timestamps"
    as_is = len(check_running_balance(txns))
    flipped = len(check_running_balance(txns[::-1]))
    if as_is != flipped:
        return (txns if as_is < flipped else txns[::-1]), "balance_chain"
    return txns[::-1], "default"


def _fit_cells(cells: list[str], header: list[str], line: int) -> list[str]:
    """Align one data row to the header.

    The bank never quotes fields, so a Description containing commas spills into extra
    cells: the surplus is rejoined into Description and the columns after it shift back.
    A row too short to reach every required column is refused.
    """
    if len(cells) > len(header):
        surplus = len(cells) - len(header)
        d = header.index("Description")
        cells = cells[:d] + [",".join(cells[d : d + surplus + 1])] + cells[d + surplus + 1 :]
    needed = header.index("Balance") + 1
    if len(cells) < needed:
        raise BankParseError(
            f"file line {line}: row has {len(cells)} cells, needs at least {needed} "
            "(through the Balance column)"
        )
    return cells


def _total(fn: Callable[[], _T]) -> _T:
    """Spec S9: run `fn`; a BankParseError passes through; any other exception is logged
    once by its type name only and becomes a fixed-text BankParseError with no cause chain."""
    failure: str | None = None
    try:
        return fn()
    except BankParseError:
        raise
    except Exception as exc:  # the net: nothing else may leave the parser
        failure = type(exc).__name__
    logger.error("bank_parser: unexpected error while parsing a CIMB statement (%s)", failure)
    raise BankParseError("statement could not be parsed")


def parse_cimb_statement(content: str) -> ParsedStatement:
    """Parse a CIMB OCTO Biz statement export; transactions come back oldest first.

    Total by construction (spec S9): returns a ParsedStatement or raises BankParseError,
    never another exception type.
    """
    return _total(lambda: _parse_cimb_statement(content))


def preview_cimb_statement(content: str) -> dict[str, object]:
    """Parse and summarise in one total call: what the preview endpoint runs (spec S8, S9)."""
    return _total(lambda: build_preview_report(_parse_cimb_statement(content)))


def _read_rows(content: str) -> list[list[str]]:
    """Text -> csv rows with fixed-text, line-numbered errors (spec S1)."""
    text = content.removeprefix("\ufeff")  # exactly one BOM is tolerated
    if text.startswith("\ufeff"):
        raise BankParseError("file line 1: more than one byte order mark")
    # a line is a run of characters ended by a newline, plus a final unterminated one
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if lines > _MAX_STATEMENT_LINES:
        raise BankParseError("statement has too many lines")
    unreadable = "unreadable as CSV (a field is too large or the text is malformed)"
    # A NUL or a lone CR refuses the file, whatever the csv module does: the earliest one.
    flat = text.replace("\r\n", "\n")
    found = [at for at in (flat.find("\x00"), flat.find("\r")) if at >= 0]
    if found:
        raise BankParseError(f"file line {flat.count(chr(10), 0, min(found)) + 1}: {unreadable}")
    # QUOTE_NONE: the bank never quotes, so a quote character is plain text. With the
    # default dialect one stray quote merges every following line into a single cell.
    reader = csv.reader(io.StringIO(text), quoting=csv.QUOTE_NONE)
    rows: list[list[str]] = []
    try:
        for row in reader:
            rows.append(row)
    except csv.Error:
        # the csv module's own text can quote the file: say it in fixed words
        raise BankParseError(f"file line {reader.line_num}: {unreadable}") from None
    return rows


def parse_and_preview_cimb_statement(content: str) -> tuple[ParsedStatement, dict[str, object]]:
    """Parse and summarise in one total call, keeping the statement for the importer."""

    def run() -> tuple[ParsedStatement, dict[str, object]]:
        stmt = _parse_cimb_statement(content)
        return stmt, build_preview_report(stmt)

    return _total(run)


def _parse_cimb_statement(content: str) -> ParsedStatement:
    rows = _read_rows(content)
    header_idx = next(
        (
            i
            for i, r in enumerate(rows[:_HEADER_SEARCH_LINES])
            if r and _trim(r[0]) == _CIMB_HEADER_FIRST_CELL
        ),
        None,
    )
    if header_idx is None:
        raise BankParseError("no 'Post Date' header row found: not a CIMB statement export")

    header = [_trim(c) for c in rows[header_idx]]
    if tuple(header) != _CIMB_HEADER:
        raise BankParseError("statement header is not the CIMB Account Statement Summary layout")
    balance_needed = header.index("Balance") + 1

    # Rows before the header are metadata (two cells in the export). A non-blank one long
    # enough to reach the Balance column is a data row that would vanish silently: refuse.
    for line, r in enumerate(rows[:header_idx], start=1):
        if not _is_blank(r) and len(r) >= balance_needed:
            raise BankParseError(f"file line {line}: data row before the header")
    # `Key,Value`: the value is everything after the first comma (the bank never quotes).
    metadata = {
        _trim(r[0]): _trim(",".join(r[1:]))
        for r in rows[:header_idx]
        if len(r) >= 2 and _trim(r[0])
    }

    txns: list[ParsedTxn] = []
    in_footer = False
    for offset, cells in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if _is_blank(cells):
            # the table ends at the FIRST blank row, even if no data row was read yet
            if not txns:
                raise BankParseError("statement header found but no transaction rows")
            in_footer = True
            continue
        if in_footer:
            # Trailer ("Generated By/On"): short rows. A row long enough to reach the
            # Balance column is data-shaped and would be silently dropped: refuse the file.
            if len(cells) >= balance_needed:
                raise BankParseError(f"file line {offset}: data row after a blank line")
            continue
        cells = _fit_cells(cells, header, offset)
        row = {h: _trim(cells[i]) if i < len(cells) else "" for i, h in enumerate(header)}
        try:
            posted_at = _parse_cimb_timestamp(row["Post Date"])
            debit = _parse_decimal(row["Debit Amount"], "Debit Amount")
            credit = _parse_decimal(row["Credit Amount"], "Credit Amount")
            balance = _parse_decimal(row["Balance"], "Balance", allow_minus=True)
            if debit > 0 and credit > 0:
                raise BankParseError("row has both a debit and a credit amount")
        except BankParseError as exc:
            raise BankParseError(f"file line {offset}: {exc}") from exc
        direction = "credit" if credit > 0 else "debit"
        txns.append(
            ParsedTxn(
                txn_date=posted_at.date(),
                description=row["Description"],
                amount_idr=_round_int(credit if credit > 0 else debit),
                direction=direction,
                running_balance_idr=_round_int(balance),
                raw_row=row,
                posted_at=posted_at,
                debit=debit,
                credit=credit,
                balance=balance,
                # a row that moves no money has no kind to speak of (spec S3, S5)
                kind=(
                    classify_cimb_kind(row["Payment Type"], row["Description"], direction)
                    if debit > 0 or credit > 0
                    else "other"
                ),
            )
        )
    if not txns:
        raise BankParseError("statement header found but no transaction rows")

    txns, decided_by = _orient_oldest_first(txns)
    return ParsedStatement(
        account_number=metadata.get("Account No.", ""),
        account_name=metadata.get("Account Name", ""),
        metadata=metadata,
        transactions=txns,
        order_decided_by=decided_by,
    )


def check_running_balance(txns: list[ParsedTxn]) -> list[int]:
    """Positions (0-based, oldest-first) whose balance != previous balance + credit - debit.

    Position 0 has no predecessor (its predecessor is the derived opening balance),
    so it is never reported. A tampered balance shows at its row and at the next one.

    This checks that the chain is INTERNALLY consistent, not that the figures are
    authentic: a changed oldest amount, or a coordinated change of amount and balance,
    passes.
    """
    bad: list[int] = []
    for i in range(1, len(txns)):
        prev, cur = txns[i - 1].balance, txns[i]
        if prev is None or cur.balance is None or prev + cur.credit - cur.debit != cur.balance:
            bad.append(i)
    return bad


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def build_preview_report(stmt: ParsedStatement) -> dict[str, object]:
    """Read-only summary of a parsed statement (amounts as 2-decimal strings)."""
    txns = stmt.transactions
    mismatches = check_running_balance(txns)
    kinds: dict[str, dict[str, object]] = {}
    for t in txns:
        slot = kinds.setdefault(t.kind, {"count": 0, "_total": Decimal(0)})
        slot["count"] = int(slot["count"]) + 1  # type: ignore[call-overload]
        slot["_total"] = slot["_total"] + t.credit + t.debit  # type: ignore[operator]
    return {
        "account": {"number": stmt.account_number, "name": stmt.account_name},
        "period": {
            "first": min(t.posted_at for t in txns).isoformat(),  # type: ignore[type-var,union-attr]
            "last": max(t.posted_at for t in txns).isoformat(),  # type: ignore[type-var,union-attr]
        },
        "row_count": len(txns),
        "opening_balance": _money(stmt.opening_balance),
        "closing_balance": _money(stmt.closing_balance),
        "total_debit": _money(sum((t.debit for t in txns), Decimal(0))),
        "total_credit": _money(sum((t.credit for t in txns), Decimal(0))),
        "balance_check": {"ok": not mismatches, "mismatch_positions": mismatches},
        "order_decided_by": stmt.order_decided_by,
        "kinds": {
            k: {"count": v["count"], "total": _money(v["_total"])}  # type: ignore[arg-type]
            for k, v in sorted(kinds.items())
        },
    }
