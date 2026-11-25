"""Done today: the transfers the accountant handled in the current WITA day. Read-only.

One SELECT over reconciliation_log. A transfer counts once, under its newest log row, and
only while its current status is matched, manual or ignored, or unmatched with a standing
confirmation (a partly applied transfer); a reopened transfer is back in the inbox, so it is not "done". The reason typed for an ignore is stored but never returned.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from backend.services.accounting.period import month_condition
from backend.services.accounting.reconcile_service import STANDING_CONFIRMATION_SQL

BUSINESS_TZ = ZoneInfo("Asia/Makassar")

# Items returned at most; counts still cover every handled transfer.
CAP = 500

_ACTION_OF_STATUS = {
    "matched": "confirmed",
    "manual": "linked",
    "ignored": "ignored",
    "unmatched": "confirmed",  # partly applied: the SQL admits an unmatched row only then
}

# A partly applied transfer is `unmatched` with no ids on the bank row: it is "confirmed", and its
# invoice and client are those of its newest standing confirmation, read from that log row. A
# closed transfer reads the same row; a linked one (no confirmation) falls back to the bank row.
DONE_TODAY_SQL = f"""
SELECT bt.id AS txn_id, bt.txn_date, bt.amount_idr, bt.description,
       bt.reconciled_status, l.confirmed_at,
       i.invoice_number, c.full_name AS client_name
FROM reconciliation_log l
JOIN bank_transactions bt ON bt.id = l.bank_txn_id
LEFT JOIN LATERAL (
    SELECT r.practice_id, r.invoice_ids FROM reconciliation_log r
    WHERE r.bank_txn_id = bt.id AND {STANDING_CONFIRMATION_SQL}
    ORDER BY r.id DESC LIMIT 1
) s ON true
LEFT JOIN invoices i ON i.id = COALESCE((s.invoice_ids->>0)::bigint, bt.matched_invoice_id)
LEFT JOIN practices p ON p.id = COALESCE(s.practice_id, bt.matched_practice_id)
LEFT JOIN clients c ON c.id = p.client_id
WHERE l.confirmed_at >= $1 AND l.confirmed_at < $2
  AND bt.direction = 'credit'{{period}}
  AND (bt.reconciled_status IN ('matched', 'manual', 'ignored')
       OR (bt.reconciled_status = 'unmatched' AND s.practice_id IS NOT NULL))
  AND NOT EXISTS (
      SELECT 1 FROM reconciliation_log later
      WHERE later.bank_txn_id = l.bank_txn_id AND later.id > l.id)
ORDER BY l.confirmed_at DESC, bt.id DESC
"""


def window_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[00:00, next 00:00) of NOW's WITA calendar date, as timezone-aware datetimes."""
    day = now.astimezone(BUSINESS_TZ).date()
    start = datetime.combine(day, time.min, tzinfo=BUSINESS_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
    return start, end


def _item(row: Any, action: str) -> dict[str, Any]:
    handled = action != "ignored"
    return {
        "txn_id": int(row["txn_id"]),
        "action": action,
        "done_at": row["confirmed_at"].astimezone(BUSINESS_TZ).isoformat(timespec="seconds"),
        "txn_date": str(row["txn_date"]),
        "amount_idr": int(row["amount_idr"]),
        "description": row["description"] or "",
        "invoice_number": row["invoice_number"] if handled else None,
        "client_name": row["client_name"] if handled else None,
    }


async def load_done_today(
    conn: asyncpg.Connection, now: datetime, *, month: date | None = None
) -> dict[str, Any]:
    start, end = window_bounds(now)
    params = [start, end]
    period = month_condition("bt.txn_date", month, params)
    rows = await conn.fetch(DONE_TODAY_SQL.format(period=period), *params)
    counts = {"confirmed": 0, "linked": 0, "ignored": 0}
    items = []
    for row in rows:
        action = _ACTION_OF_STATUS[row["reconciled_status"]]
        counts[action] += 1
        if len(items) < CAP:
            items.append(_item(row, action))
    total = sum(counts.values())
    return {
        "day": start.date().isoformat(),
        "timezone": "Asia/Makassar",
        "counts": {**counts, "total": total},
        "items": items,
        "truncated": total > len(items),
    }
