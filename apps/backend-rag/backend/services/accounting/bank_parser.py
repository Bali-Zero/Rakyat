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
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)


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

_CIMB_HEADER_FIRST_CELL = "post date"
_CIMB_REQUIRED_COLUMNS = (
    "Post Date", "Payment Type", "Debit Amount", "Credit Amount", "Balance", "Description",
)
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
_CLIENT_CREDIT_PREFIXES = ("REMITTANCE CR", "OVERBOOKING CR", "OVERBOOKING FROM CA")


@dataclass
class ParsedStatement:
    account_number: str
    account_name: str
    metadata: dict[str, str]
    transactions: list[ParsedTxn]  # oldest first

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
    ptype = " ".join((payment_type or "").upper().split())
    if ptype == "BILL PAYMENT" and (description or "").strip().upper().startswith("EMPN BILLING"):
        kind = "government_fee"
    elif ptype.startswith(_CLIENT_CREDIT_PREFIXES):
        kind = "client_credit"
    else:
        kind = _KIND_BY_PAYMENT_TYPE.get(ptype, "other")
    if direction == "debit" and kind in _CREDIT_ONLY_KINDS:
        return "other"
    if direction == "credit" and kind in _DEBIT_ONLY_KINDS:
        return "other"
    return kind


def _parse_cimb_timestamp(raw: str) -> datetime:
    """'17 Nov 2026 15:31:06' -> datetime (locale-independent month names)."""
    parts = _clean_cell(raw).split()
    try:
        day, mon, year = int(parts[0]), _MONTHS[parts[1][:3].lower()], int(parts[2])
        hh, mm, ss = (int(x) for x in (parts[3] if len(parts) > 3 else "0:0:0").split(":"))
        return datetime(year, mon, day, hh, mm, ss)
    except (IndexError, KeyError, ValueError) as exc:
        raise BankParseError(f"cannot parse timestamp {raw!r}") from exc


# No leading zeros: an unquoted thousands comma ("1,000.00") splits into "1" and "000.00".
_AMOUNT_RE = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?")
_SIGNED_AMOUNT_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?")


def _parse_decimal(raw: str, what: str, allow_minus: bool = False) -> Decimal:
    """Strict plain decimal; the pattern is checked BEFORE any numeric conversion."""
    s = _clean_cell(raw)
    pattern = _SIGNED_AMOUNT_RE if allow_minus else _AMOUNT_RE
    if not pattern.fullmatch(s):
        shown = s if len(s) <= 40 else s[:40] + "..."
        raise BankParseError(f"{what} {shown!r} is not a plain decimal amount")
    return Decimal(s)


def _round_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _orient_oldest_first(txns: list[ParsedTxn]) -> list[ParsedTxn]:
    """Return rows oldest-first. The running-balance chain decides; the order with fewer
    chain mismatches wins. Tie: timestamps (first later than last -> reverse, earlier ->
    keep); still tied: reverse, because the export is documented newest-first."""
    as_is = len(check_running_balance(txns))
    flipped = len(check_running_balance(txns[::-1]))
    if as_is != flipped:
        return txns if as_is < flipped else txns[::-1]
    first, last = txns[0].posted_at, txns[-1].posted_at
    if first < last:  # type: ignore[operator]
        return txns
    return txns[::-1]


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
    needed = max(header.index(c) for c in _CIMB_REQUIRED_COLUMNS if c != "Description") + 1
    if len(cells) < needed:
        raise BankParseError(
            f"file line {line}: row has {len(cells)} cells, needs at least {needed} "
            "(through the Balance column)"
        )
    return cells


def parse_cimb_statement(content: str) -> ParsedStatement:
    """Parse a CIMB OCTO Biz statement export; transactions come back oldest first."""
    try:
        # QUOTE_NONE: the bank never quotes, so a quote character is plain text. With the
        # default dialect one stray quote merges every following line into a single cell.
        rows = list(csv.reader(io.StringIO(content.lstrip("\ufeff")), quoting=csv.QUOTE_NONE))
    except csv.Error as exc:
        raise BankParseError(f"statement is unreadable as CSV: {exc}") from exc
    header_idx = next(
        (i for i, r in enumerate(rows) if r and r[0].strip().lower() == _CIMB_HEADER_FIRST_CELL),
        None,
    )
    if header_idx is None:
        raise BankParseError("no 'Post Date' header row found: not a CIMB statement export")

    metadata = {r[0].strip(): r[1].strip() for r in rows[:header_idx] if len(r) >= 2 and r[0].strip()}
    header = [c.strip() for c in rows[header_idx]]
    missing = [c for c in _CIMB_REQUIRED_COLUMNS if c not in header]
    if missing:
        raise BankParseError(f"statement header lacks column(s): {missing}")

    txns: list[ParsedTxn] = []
    in_footer = False
    for offset, cells in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if not any(c.strip() for c in cells):
            in_footer = bool(txns)  # a blank line after the data closes the table
            continue
        if in_footer:
            # "Generated By/On" trailer. A real data row here would be silently
            # dropped, so refuse the file instead.
            try:
                _parse_cimb_timestamp(cells[0])
            except BankParseError:
                continue
            raise BankParseError(f"file line {offset}: data row after a blank line")
        cells = _fit_cells(cells, header, offset)
        row = {h: _clean_cell(cells[i]) if i < len(cells) else "" for i, h in enumerate(header)}
        try:
            posted_at = _parse_cimb_timestamp(row["Post Date"])
            debit = _parse_decimal(row["Debit Amount"], "debit")
            credit = _parse_decimal(row["Credit Amount"], "credit")
            balance = _parse_decimal(row["Balance"], "balance", allow_minus=True)
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
                kind=classify_cimb_kind(row["Payment Type"], row["Description"], direction),
            )
        )
    if not txns:
        raise BankParseError("statement header found but no transaction rows")

    txns = _orient_oldest_first(txns)
    return ParsedStatement(
        account_number=metadata.get("Account No.", ""),
        account_name=metadata.get("Account Name", ""),
        metadata=metadata,
        transactions=txns,
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
            "first": txns[0].posted_at.isoformat(),  # type: ignore[union-attr]
            "last": txns[-1].posted_at.isoformat(),  # type: ignore[union-attr]
        },
        "row_count": len(txns),
        "opening_balance": _money(stmt.opening_balance),
        "closing_balance": _money(stmt.closing_balance),
        "total_debit": _money(sum((t.debit for t in txns), Decimal(0))),
        "total_credit": _money(sum((t.credit for t in txns), Decimal(0))),
        "balance_check": {"ok": not mismatches, "mismatch_positions": mismatches},
        "kinds": {
            k: {"count": v["count"], "total": _money(v["_total"])}  # type: ignore[arg-type]
            for k, v in sorted(kinds.items())
        },
    }
