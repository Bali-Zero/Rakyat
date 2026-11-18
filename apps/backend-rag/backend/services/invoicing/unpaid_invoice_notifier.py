"""
Unpaid Invoice Notifier.

Owner decision 2026-11-13: when an invoice issued by the new invoicing flow is
still unpaid 3 days after issue, accounting (Asya) gets ONE digest email
naming it, once per invoice. The backlog issued before the new flow is never
emailed.

Rules, each pinned by a test in tests/unit/services/invoicing/
test_unpaid_invoice_notifier.py (and the Postgres twin test):
  * issue date = invoices.created_at (first issue; a regeneration upserts the
    row and never moves it);
  * only invoices created at or after INVOICE_REMINDER_FROM (2026-11-11 00:00 WITA);
  * only while the practice payment_status is unpaid or partial, and the
    practice is not cancelled;
  * each invoice is reminded once: invoice_payment_reminders (migration 360)
    is recorded only AFTER the mail was delivered, under a Postgres advisory
    lock so two overlapping runs cannot both send;
  * the digest carries no client data: invoice number, service, amount,
    days unpaid, team member.
"""

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from backend.app.services.internal_email import send_internal_email
from backend.app.utils.logging_utils import get_logger
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL

logger = get_logger(__name__)

# Days after issue before an unpaid invoice is reminded.
REMINDER_DAYS: int = 3

# First WITA calendar day of the new invoicing flow (2026-11-11 00:00 WITA).
WITA = timezone(timedelta(hours=8))
INVOICE_REMINDER_FROM: datetime = datetime(2026, 11, 11, 0, 0, tzinfo=WITA)

EMAIL_TYPE = "unpaid_invoice_digest"
# system_settings row rewritten on EVERY run: the persisted proof that the job executed.
HEARTBEAT_KEY = "unpaid_invoice_reminder_last_run"

# Held for the whole run (check, send, record) so overlapping runs serialise.
_ADVISORY_LOCK_KEY = 7_360_001

_DUE_SQL = """
    SELECT
        i.id                                            AS invoice_id,
        i.practice_id,
        i.invoice_number,
        i.amount_idr,
        i.paid_amount_idr,
        p.payment_status,
        p.assigned_to,
        COALESCE(pt.name, 'Service')                    AS service_name,
        EXTRACT(DAY FROM NOW() - i.created_at)::int     AS days_unpaid
    FROM invoices i
    JOIN practices p            ON p.id = i.practice_id
    LEFT JOIN practice_types pt ON pt.id = p.practice_type_id
    WHERE i.created_at >= $1
      AND i.created_at <= NOW() - ($2 * INTERVAL '1 day')
      AND p.payment_status IN ('unpaid', 'partial')
      AND p.status <> 'cancelled'
      AND NOT EXISTS (
            SELECT 1 FROM invoice_payment_reminders r WHERE r.invoice_id = i.id
      )
    ORDER BY i.created_at ASC
"""


def _fmt_idr(value: Any) -> str:
    return f"Rp {float(value or 0):,.0f}".replace(",", ".")


def _build_digest(invoices: list[dict[str, Any]], today: str) -> tuple[str, str]:
    n = len(invoices)
    subject = (
        f"[UNPAID] {n} invoice{'s' if n != 1 else ''} unpaid after {REMINDER_DAYS} days - {today}"
    )
    cell = "padding:8px 12px;border-bottom:1px solid #eee;"
    rows = []
    for inv in invoices:
        amount = _fmt_idr(inv.get("amount_idr"))
        if inv.get("payment_status") == "partial":
            outstanding = float(inv.get("amount_idr") or 0) - float(inv.get("paid_amount_idr") or 0)
            amount += f" (partial, outstanding {_fmt_idr(max(outstanding, 0))})"
        rows.append(
            "<tr>"
            f"<td style='{cell}'>{html.escape(inv.get('invoice_number') or '-')}</td>"
            f"<td style='{cell}'>{html.escape(inv.get('service_name') or '-')}</td>"
            f"<td style='{cell}text-align:right;'>{html.escape(amount)}</td>"
            f"<td style='{cell}text-align:center;'><strong>{int(inv.get('days_unpaid') or 0)}</strong></td>"
            f"<td style='{cell}'>{html.escape(inv.get('assigned_to') or '-')}</td>"
            "</tr>"
        )
    body = f"""
<div style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;color:#333;">
  <h2 style="color:#363A3E;">Invoices still unpaid</h2>
  <p>Hi Asya, {n} invoice{"s are" if n != 1 else " is"} still unpaid {REMINDER_DAYS} days after issue.
  Each invoice appears in this reminder once.</p>
  <table style="width:100%;border-collapse:collapse;margin:16px 0 24px;font-size:14px;">
    <thead>
      <tr style="background:#363A3E;color:#fff;">
        <th style="padding:10px 12px;text-align:left;">Invoice #</th>
        <th style="padding:10px 12px;text-align:left;">Service</th>
        <th style="padding:10px 12px;text-align:right;">Amount</th>
        <th style="padding:10px 12px;text-align:center;">Days unpaid</th>
        <th style="padding:10px 12px;text-align:left;">Team member</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <p>Please check the bank account; if a payment has arrived, confirm it on the Accounting page: <a href="https://kita.balizero.com/accounting">https://kita.balizero.com/accounting</a>.</p>
  <p style="color:#888;font-size:13px;">Zantara CRM</p>
</div>
""".strip()
    return subject, body


class UnpaidInvoiceNotifier:
    """Sends accounting one digest of invoices unpaid after REMINDER_DAYS."""

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    async def check_and_notify(self) -> dict[str, Any]:
        """Run once. Returns due_count / notified / skipped (+ error)."""
        logger.info(
            "[UnpaidInvoiceNotifier] run start (threshold %s days, from %s)",
            REMINDER_DAYS,
            INVOICE_REMINDER_FROM.isoformat(),
        )
        try:
            async with self.db_pool.acquire(timeout=30) as conn:
                if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK_KEY):
                    logger.info("[UnpaidInvoiceNotifier] another run holds the lock - skipped")
                    return {"due_count": 0, "notified": False, "skipped": True, "locked": True}
                try:
                    try:
                        result = await self._run_locked(conn)
                    except Exception as exc:
                        logger.error(
                            "[UnpaidInvoiceNotifier] run failed: %s",
                            type(exc).__name__,
                            exc_info=True,
                        )
                        result = {
                            "due_count": 0,
                            "notified": False,
                            "skipped": False,
                            "error": type(exc).__name__,
                        }
                    await self._write_heartbeat(conn, result)
                    return result
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)
        except Exception as exc:
            logger.error(
                "[UnpaidInvoiceNotifier] run failed: %s", type(exc).__name__, exc_info=True
            )
            return {
                "due_count": 0,
                "notified": False,
                "skipped": False,
                "error": type(exc).__name__,
            }

    async def _run_locked(self, conn: asyncpg.Connection) -> dict[str, Any]:
        rows = await conn.fetch(_DUE_SQL, INVOICE_REMINDER_FROM, REMINDER_DAYS)
        invoices = [dict(r) for r in rows]
        if not invoices:
            logger.info("[UnpaidInvoiceNotifier] run done: nothing due, no email")
            return {"due_count": 0, "notified": False, "skipped": True}

        today = datetime.now(tz=WITA).strftime("%d %B %Y")
        subject, body = _build_digest(invoices, today)
        delivered = await send_internal_email(
            to=ACCOUNTING_EMAIL,
            subject=subject,
            body=body,
            log_context=f"unpaid_invoice_digest n={len(invoices)}",
            email_type=EMAIL_TYPE,
            pool=self.db_pool,
        )
        if not delivered:
            logger.error(
                "[UnpaidInvoiceNotifier] digest not delivered (%s due) - will retry next run",
                len(invoices),
            )
            return {
                "due_count": len(invoices),
                "notified": False,
                "skipped": False,
                "error": "send_failed",
            }

        await conn.executemany(
            "INSERT INTO invoice_payment_reminders (invoice_id, days_unpaid) VALUES ($1, $2) "
            "ON CONFLICT (invoice_id) DO NOTHING",
            [(inv["invoice_id"], int(inv["days_unpaid"] or 0)) for inv in invoices],
        )
        logger.info(
            "[UnpaidInvoiceNotifier] run done: digest sent for %s invoice(s)", len(invoices)
        )
        return {"due_count": len(invoices), "notified": True, "skipped": False}

    async def _write_heartbeat(self, conn: asyncpg.Connection, result: dict[str, Any]) -> None:
        payload = json.dumps(
            {
                "ran_at": datetime.now(tz=timezone.utc).isoformat(),
                "due_count": result.get("due_count", 0),
                "notified": bool(result.get("notified")),
                "error": result.get("error"),
            }
        )
        try:
            await conn.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                HEARTBEAT_KEY,
                payload,
            )
        except Exception as exc:
            logger.warning("[UnpaidInvoiceNotifier] heartbeat write failed: %s", type(exc).__name__)
