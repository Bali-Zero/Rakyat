"""Build invented CIMB statements for the import tests (no real bank data anywhere).

`build_statement` takes rows oldest-first, writes the export newest-first with a
running balance computed from `opening`, and parses it with the real parser, so the
tests exercise the same ParsedStatement the endpoint produces.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from backend.services.accounting.bank_parser import ParsedStatement, parse_cimb_statement

HEADER = (
    "Post Date,Payment Date,Payment Type,Other Reference,Bank Reference,"
    "Debit Amount,Credit Amount,Balance,Description,Cheque Number"
)
ACCOUNT = "700000000001"

Row = tuple[datetime, str, str, str]  # (posted_at, payment type, debit, credit)


def statement_text(
    rows: list[Row],
    *,
    opening: str,
    account: str | None = ACCOUNT,
    tamper_balance_of: int | None = None,
) -> str:
    balance = Decimal(opening)
    lines: list[str] = []
    for i, (at, ptype, debit, credit) in enumerate(rows):
        balance = balance + Decimal(credit) - Decimal(debit)
        shown = balance + (1 if tamper_balance_of == i else 0)
        lines.append(
            f"{at:%d %b %Y %H:%M:%S},,{ptype},REF{i},BRF{i},{debit},{credit},{shown:.2f},desc{i},"
        )
    meta = f"Account No.,{account}\n" if account is not None else ""
    return f"{meta}Account Name,PT CONTOH UJI\n\n{HEADER}\n" + "\n".join(reversed(lines)) + "\n"


def build_statement(rows: list[Row], *, opening: str, **kw: object) -> ParsedStatement:
    return parse_cimb_statement(statement_text(rows, opening=opening, **kw))  # type: ignore[arg-type]


def at(day: int, hh: int = 9, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, ss)
