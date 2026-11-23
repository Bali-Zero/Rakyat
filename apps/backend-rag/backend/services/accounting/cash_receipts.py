"""Invoice-backed cash receipts in the same register as reconciled transfers."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from backend.services.accounting.done_today_service import BUSINESS_TZ
from backend.services.accounting.reconcile_service import (
    INVOICE_BALANCE_SQL,
    ReconcileError,
    confirm_payment,
)
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES

_COLLECTIBLE = """
    COALESCE(i.issuer_entity_key, $2) = $1
    AND COALESCE(p.payment_status, 'unpaid') <> 'paid'
    AND p.status IS DISTINCT FROM 'cancelled'
    AND i.amount_idr > 0 AND i.amount_idr < 'Infinity'::numeric
"""


def _check_company(key: str) -> None:
    if key not in ENTITIES:
        raise ReconcileError("Select an issuing company.")


async def cash_invoices(conn: asyncpg.Connection, company_key: str, q: str) -> list[dict]:
    _check_company(company_key)
    rows = await conn.fetch(
        f"""SELECT i.id AS invoice_id, i.invoice_number, p.id AS practice_id,
                   COALESCE(b.company_name, c.full_name, '') AS client_name,
                   pt.name AS service_name, i.amount_idr, ({INVOICE_BALANCE_SQL}) AS outstanding_idr
            FROM invoices i JOIN practices p ON p.id = i.practice_id
            LEFT JOIN companies b ON b.id = p.invoice_company_id
            LEFT JOIN clients c ON c.id = p.client_id
            LEFT JOIN practice_types pt ON pt.id = p.practice_type_id
            WHERE {_COLLECTIBLE} AND ({INVOICE_BALANCE_SQL}) > 0
              AND ({INVOICE_BALANCE_SQL}) < 'Infinity'::numeric
              AND strpos(lower(concat_ws(' ', i.invoice_number, b.company_name, c.full_name)),
                         lower($3)) > 0
            ORDER BY i.generated_at DESC NULLS LAST, i.id DESC LIMIT 30""",
        company_key,
        DEFAULT_ENTITY_KEY,
        q.strip(),
    )
    return [dict(row) for row in rows]


async def record_cash(
    conn: asyncpg.Connection,
    *,
    company_key: str,
    invoice_id: int,
    amount_idr: int,
    movement_date: date,
    reference: str | None,
    request_id: UUID,
    confirmed_by: str,
) -> dict:
    _check_company(company_key)
    if amount_idr <= 0 or movement_date > datetime.now(BUSINESS_TZ).date():
        raise ReconcileError("Enter a positive amount and a receipt date no later than today.")
    reference = (reference or "").strip() or None
    async with conn.transaction():
        # Serialize even when the UUID is retried against a different invoice.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", str(request_id))
        old = await conn.fetchrow(
            """SELECT id, cash_company_key, linked_invoice_id, amount_idr, movement_date,
                      description, cash_balance_after_idr FROM weekly_cashout WHERE cash_request_id=$1""",
            request_id,
        )
        if old:
            if (
                old["cash_company_key"],
                old["linked_invoice_id"],
                old["amount_idr"],
                old["movement_date"],
                old["description"],
            ) != (company_key, invoice_id, amount_idr, movement_date, reference):
                raise ReconcileError(
                    "This receipt reference was already used for a different payment."
                )
            return _receipt(old["id"], old["cash_balance_after_idr"])

        # Same lock order as bank reconciliation. Read balances in a fresh statement
        # after taking the practice lock, including any payment that was ahead of us.
        practice_id = await conn.fetchval(
            """SELECT p.id FROM practices p JOIN invoices i ON i.practice_id=p.id
               WHERE i.id=$1 FOR UPDATE OF p""",
            invoice_id,
        )
        invoice = await conn.fetchrow(
            f"""SELECT {INVOICE_BALANCE_SQL} AS balance,
                       GREATEST(COALESCE(i.paid_amount_idr, 0), COALESCE(p.paid_amount, 0)) AS paid
                FROM invoices i JOIN practices p ON p.id=i.practice_id
                WHERE {_COLLECTIBLE} AND i.id=$3 AND p.id=$4 FOR UPDATE OF i""",
            company_key,
            DEFAULT_ENTITY_KEY,
            invoice_id,
            practice_id,
        )
        balance = invoice["balance"] if invoice else None
        if balance is None or not Decimal(balance).is_finite() or balance <= 0:
            raise ReconcileError(
                "There is no outstanding invoice for this company. Refresh the invoice list."
            )
        if balance != int(balance):
            raise ReconcileError(
                "The invoice balance requires review before recording whole rupiah."
            )
        if amount_idr > balance:
            raise ReconcileError(
                "The amount exceeds the current invoice balance. Refresh the invoice list."
            )
        remaining = int(balance) - amount_idr
        result = await confirm_payment(
            conn,
            bank_txn_id=None,
            practice_id=practice_id,
            invoice_id=invoice_id,
            amount_applied_idr=amount_idr,
            new_status="partial" if remaining else "paid",
            confirmed_by=confirmed_by,
            payment_method="cash",
            payment_reference=reference,
            movement_date=movement_date,
            prior_recorded_idr=int(invoice["paid"]),
        )
        await conn.execute(
            """UPDATE weekly_cashout SET cash_request_id=$1, cash_company_key=$2,
                      cash_balance_after_idr=$3, description=$4 WHERE id=$5""",
            request_id,
            company_key,
            remaining,
            reference,
            result.cashout_id,
        )
        return _receipt(result.cashout_id, remaining)


def _receipt(row_id: int, remaining: int) -> dict:
    return {
        "cashout_id": row_id,
        "status_after": "partial" if remaining else "paid",
        "outstanding_idr": remaining,
    }
