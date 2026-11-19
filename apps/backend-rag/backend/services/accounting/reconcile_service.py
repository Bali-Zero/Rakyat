"""Reconciliation confirm service — the single writer of payment state.

When Asya confirms a match, this is the ONLY path that mutates
practices.payment_status / paid_amount (superscar #9: never a side-channel).
Everything happens in one transaction:
  1. update practices (payment_status, paid_amount) — same columns the CRM PATCH uses
  2. fill invoices (paid_date, payment_method, payment_reference, paid_amount_idr)
  3. mark bank_transactions reconciled (4-state) + link practice/invoice
  4. insert a weekly_cashout row (the money-log, with price decomposition)
  5. insert an immutable reconciliation_log row (audit)
Then invalidate the same cache keys the CRM uses.

Cash-basis, IDR. The accountant has already chosen the match; this records it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import asyncpg

from backend.services.accounting.matcher import EXACT_TOLERANCE_IDR

logger = logging.getLogger(__name__)

# Mirrors crm_practices PAYMENT_STATUS_VALUES — keep in sync.
PAYMENT_STATUS_VALUES = ("unpaid", "partial", "paid")


@dataclass
class ConfirmResult:
    practice_id: int
    invoice_id: int | None
    cashout_id: int
    reconciliation_log_id: int
    status_before: str | None
    status_after: str


class ReconcileError(Exception):
    """Raised on an invalid confirm request (bad status, missing practice)."""


def _iso_week_label(d: date) -> str:
    """A human week label like Asya's tabs, derived from ISO week."""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


async def confirm_payment(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int | None,
    practice_id: int,
    invoice_id: int | None,
    amount_applied_idr: int,
    new_status: str,
    confirmed_by: str,
    payment_method: str = "bank_transfer",
    payment_reference: str | None = None,
    pph_withheld_idr: int = 0,
    bank_fee_idr: int = 0,
    # price decomposition (Asya's sheet) — optional, defaults to 0
    pnbp_idr: int = 0,
    urgent_idr: int = 0,
    rptka_imta_idr: int = 0,
    margin_idr: int = 0,
    movement_date: date | None = None,
) -> ConfirmResult:
    """Confirm a payment match in one transaction. Returns the audit ids.

    `new_status` must be one of PAYMENT_STATUS_VALUES. `amount_applied_idr` is
    what actually landed (net of withholding/fees); the gross is recorded via
    the decomposition + pph/fee fields.
    """
    if new_status not in PAYMENT_STATUS_VALUES:
        raise ReconcileError(
            f"new_status must be one of {PAYMENT_STATUS_VALUES}, got {new_status!r}"
        )

    # D4 guard: the price decomposition must reconcile to the recorded final
    # price. final_price_idr is computed below as the sum; if the caller also
    # supplied a decomposition, an internally inconsistent row is a real bug.
    decomposition_sum = pnbp_idr + urgent_idr + rptka_imta_idr + margin_idr
    if decomposition_sum > 0 and amount_applied_idr > 0 and decomposition_sum != amount_applied_idr:
        # not fatal (amount may be net of withholding/fees), but warn loudly
        logger.warning(
            "reconcile: decomposition sum %s != amount_applied %s (practice=%s) — "
            "expected if PPh/fee withheld (pph=%s fee=%s)",
            decomposition_sum, amount_applied_idr, practice_id, pph_withheld_idr, bank_fee_idr,
        )

    # D5 guard: a 'paid' status with an amount far below a single invoice is a
    # likely fat-finger — surface it (the accountant still owns the decision).
    if new_status == "paid" and invoice_id is not None:
        logger.info(
            "reconcile: marking practice %s PAID via invoice %s for %s IDR by %s",
            practice_id, invoice_id, amount_applied_idr, confirmed_by,
        )

    async with conn.transaction():
        # 0. A bank transaction is confirmed as a payment at most once: lock its row FIRST,
        #    so a concurrent confirmation of the same row waits and then sees it matched.
        if bank_txn_id is not None:
            bank = await conn.fetchrow(
                "SELECT direction, reconciled_status FROM bank_transactions WHERE id = $1 FOR UPDATE",
                bank_txn_id,
            )
            if bank is None:
                raise ReconcileError(f"bank transaction {bank_txn_id} not found")
            if bank["direction"] != "credit":
                raise ReconcileError(f"bank transaction {bank_txn_id} is not a credit")
            if bank["reconciled_status"] != "unmatched":
                raise ReconcileError(f"bank transaction {bank_txn_id} is not unmatched")

        # 1. Read current practice state (lock the row) — verify it exists.
        prac = await conn.fetchrow(
            "SELECT id, payment_status, paid_amount FROM practices WHERE id = $1 FOR UPDATE",
            practice_id,
        )
        if prac is None:
            raise ReconcileError(f"practice {practice_id} not found")
        status_before = prac["payment_status"]
        prior_paid = int(prac["paid_amount"] or 0)
        new_paid = prior_paid + amount_applied_idr

        # 1b. A given invoice must exist and belong to this practice, before any write.
        invoice_amount = None
        if invoice_id is not None:
            inv = await conn.fetchrow(
                "SELECT practice_id, amount_idr FROM invoices WHERE id = $1 FOR SHARE", invoice_id
            )
            if inv is None:
                raise ReconcileError(f"invoice {invoice_id} not found")
            if inv["practice_id"] != practice_id:
                raise ReconcileError(
                    f"invoice {invoice_id} does not belong to practice {practice_id}"
                )
            invoice_amount = inv["amount_idr"]
            if invoice_amount is not None and not Decimal(invoice_amount).is_finite():
                raise ReconcileError(f"invoice {invoice_id} has no usable amount")

        # 2. Update practices — SAME columns/path the CRM PATCH writes (superscar #9).
        #    The invoice amount fills actual_price only while it is still NULL.
        await conn.execute(
            """
            UPDATE practices
            SET payment_status = $1, paid_amount = $2,
                actual_price = COALESCE(actual_price, $4),
                updated_at = now()
            WHERE id = $3
            """,
            new_status,
            new_paid,
            practice_id,
            invoice_amount,
        )

        # 3. Fill invoice payment fields, if an invoice is linked. The invoice was paid when the
        #    money landed: the transfer's date when it is known (a statement row has a date only,
        #    so midnight UTC whatever the session time zone: the same calendar day in UTC, where
        #    production reports group by month, and in Bali), the confirmation time otherwise.
        if invoice_id is not None:
            await conn.execute(
                """
                UPDATE invoices
                SET paid_date = COALESCE(($5::date)::timestamp AT TIME ZONE 'UTC', now()),
                    payment_method = $1,
                    payment_reference = $2,
                    paid_amount_idr = COALESCE(paid_amount_idr, 0) + $3
                WHERE id = $4
                """,
                payment_method,
                payment_reference,
                amount_applied_idr,
                invoice_id,
                movement_date,
            )

        # 4. Mark the bank transaction reconciled + link.
        if bank_txn_id is not None:
            await conn.execute(
                """
                UPDATE bank_transactions
                SET reconciled_status = 'matched',
                    matched_practice_id = $1,
                    matched_invoice_id = $2
                WHERE id = $3
                """,
                practice_id,
                invoice_id,
                bank_txn_id,
            )

        # 5. Insert the money-log row (weekly_cashout) with decomposition.
        if movement_date is not None:
            week = _iso_week_label(movement_date)
            cashout_id = await conn.fetchval(
                """
                INSERT INTO weekly_cashout
                    (movement_date, week_label, direction, type, amount_idr,
                     pnbp_idr, urgent_idr, rptka_imta_idr, margin_idr, final_price_idr,
                     pph_withheld_idr, bank_fee_idr,
                     linked_practice_id, linked_invoice_id, linked_bank_txn_id,
                     payment_method, recorded_by, recorded_at)
                VALUES ($1, $2, 'in', 'invoice_payment', $3,
                        $4, $5, $6, $7, $8,
                        $9, $10,
                        $11, $12, $13,
                        $14, $15, now())
                RETURNING id
                """,
                movement_date,
                week,
                amount_applied_idr,
                pnbp_idr,
                urgent_idr,
                rptka_imta_idr,
                margin_idr,
                pnbp_idr + urgent_idr + rptka_imta_idr + margin_idr,
                pph_withheld_idr,
                bank_fee_idr,
                practice_id,
                invoice_id,
                bank_txn_id,
                payment_method,
                confirmed_by,
            )
        else:
            cashout_id = await conn.fetchval(
                """
                INSERT INTO weekly_cashout
                    (movement_date, week_label, direction, type, amount_idr,
                     pnbp_idr, urgent_idr, rptka_imta_idr, margin_idr, final_price_idr,
                     pph_withheld_idr, bank_fee_idr,
                     linked_practice_id, linked_invoice_id, linked_bank_txn_id,
                     payment_method, recorded_by, recorded_at)
                VALUES (CURRENT_DATE, NULL, 'in', 'invoice_payment', $1,
                        $2, $3, $4, $5, $6,
                        $7, $8,
                        $9, $10, $11,
                        $12, $13, now())
                RETURNING id
                """,
                amount_applied_idr,
                pnbp_idr,
                urgent_idr,
                rptka_imta_idr,
                margin_idr,
                pnbp_idr + urgent_idr + rptka_imta_idr + margin_idr,
                pph_withheld_idr,
                bank_fee_idr,
                practice_id,
                invoice_id,
                bank_txn_id,
                payment_method,
                confirmed_by,
            )

        # 6. Immutable audit row.
        recon_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, invoice_ids, practice_id, cashout_id, confirmed_by,
                 confirmed_at, status_before, status_after, amount_applied_idr,
                 pph_delta_idr, bank_fee_delta_idr)
            VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8, $9, $10)
            RETURNING id
            """,
            bank_txn_id,
            # A list, never json.dumps(...): the pool's jsonb codec encodes it, and a
            # pre-encoded string would be stored as a JSON string scalar (core/pg_json_codec.py).
            [invoice_id] if invoice_id is not None else [],
            practice_id,
            cashout_id,
            confirmed_by,
            status_before,
            new_status,
            amount_applied_idr,
            pph_withheld_idr,
            bank_fee_idr,
        )

    logger.info(
        "Reconcile confirm: practice=%s invoice=%s %s->%s amount=%s by=%s recon=%s",
        practice_id, invoice_id, status_before, new_status, amount_applied_idr,
        confirmed_by, recon_id,
    )
    return ConfirmResult(
        practice_id=practice_id,
        invoice_id=invoice_id,
        cashout_id=cashout_id,
        reconciliation_log_id=recon_id,
        status_before=status_before,
        status_after=new_status,
    )


# ── Actions that move no money: link to a recorded payment, ignore, reopen ───────────────
#
# Each one is one transaction that locks the bank transaction row FIRST (as confirm_payment
# does) and checks its own preconditions on the rows it holds. None of them issues a
# statement that writes to practices, invoices or weekly_cashout. A refusal carries fixed
# text and ids: which precondition failed and the ids involved, nothing else of a row.

LINK_NOTE = "linked to a payment already recorded; no money moved"
REOPEN_NOTE = "reopened; no money moved"
REASON_MIN_LEN = 3
REASON_MAX_LEN = 200
REASON_REFUSAL = f"reason must be {REASON_MIN_LEN} to {REASON_MAX_LEN} printable characters"


@dataclass
class ActionResult:
    bank_txn_id: int
    reconciled_status: str
    reconciliation_log_id: int


async def _lock_bank_row(conn: asyncpg.Connection, bank_txn_id: int) -> asyncpg.Record:
    """First statement of every action: lock the bank transaction, refuse if it is missing."""
    bank = await conn.fetchrow(
        "SELECT direction, reconciled_status, amount_idr FROM bank_transactions WHERE id = $1 FOR UPDATE",
        bank_txn_id,
    )
    if bank is None:
        raise ReconcileError(f"bank transaction {bank_txn_id} not found")
    return bank


async def link_recorded(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int,
    practice_id: int,
    invoice_id: int,
    linked_by: str,
) -> ActionResult:
    """Link an unmatched credit to the invoice of a payment already recorded. Moves no money.

    Locks the bank transaction first and the practice second (FOR UPDATE, the statement
    confirm_payment uses), then reads the invoice under a share lock, so its amount cannot change
    under the ceiling: two links to one invoice
    name the same practice, and a link and a confirm queue on it instead of deadlocking.
    """
    async with conn.transaction():
        bank = await _lock_bank_row(conn, bank_txn_id)
        if bank["direction"] != "credit":
            raise ReconcileError(f"bank transaction {bank_txn_id} is not a credit")
        if bank["reconciled_status"] != "unmatched":
            raise ReconcileError(f"bank transaction {bank_txn_id} is not unmatched")

        prac = await conn.fetchrow(
            "SELECT payment_status FROM practices WHERE id = $1 FOR UPDATE", practice_id
        )
        if prac is None:
            raise ReconcileError(f"practice {practice_id} not found")
        if prac["payment_status"] != "paid":
            raise ReconcileError(f"practice {practice_id} has no recorded payment")

        inv = await conn.fetchrow(
            "SELECT practice_id, amount_idr FROM invoices WHERE id = $1 FOR SHARE", invoice_id
        )
        if inv is None:
            raise ReconcileError(f"invoice {invoice_id} not found")
        if inv["practice_id"] != practice_id:
            raise ReconcileError(f"invoice {invoice_id} does not belong to practice {practice_id}")
        invoice_amount = inv["amount_idr"]
        if invoice_amount is None or not Decimal(invoice_amount).is_finite():
            raise ReconcileError(f"invoice {invoice_id} has no usable amount")

        # The ceiling: what is already attached to this invoice plus this transfer may not
        # exceed the invoice's exact amount (cents included) by more than the matcher's
        # tolerance. A payment confirmed without naming its invoice is attached to the
        # invoices of its practice.
        attached = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_idr), 0) FROM bank_transactions"
            " WHERE reconciled_status IN ('matched', 'manual')"
            " AND (matched_invoice_id = $1"
            "      OR (matched_invoice_id IS NULL AND matched_practice_id = $2))",
            invoice_id,
            practice_id,
        )
        ceiling = Decimal(invoice_amount) + EXACT_TOLERANCE_IDR
        if Decimal(bank["amount_idr"]) + Decimal(attached) > ceiling:
            raise ReconcileError(
                f"bank transaction {bank_txn_id} would exceed the amount of invoice {invoice_id}"
            )

        await conn.execute(
            """
            UPDATE bank_transactions
            SET reconciled_status = 'manual',
                matched_practice_id = $1,
                matched_invoice_id = $2
            WHERE id = $3
            """,
            practice_id,
            invoice_id,
            bank_txn_id,
        )
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, invoice_ids, practice_id, confirmed_by, confirmed_at,
                 status_before, status_after, amount_applied_idr, note)
            VALUES ($1, $2, $3, $4, now(), 'paid', 'paid', 0, $5)
            RETURNING id
            """,
            bank_txn_id,
            [invoice_id],  # a list: see confirm_payment
            practice_id,
            linked_by,
            LINK_NOTE,
        )
    logger.info(
        "Reconcile link: bank_txn=%s practice=%s recon=%s", bank_txn_id, practice_id, log_id
    )
    return ActionResult(bank_txn_id, "manual", log_id)


async def ignore_transaction(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int,
    reason: str,
    ignored_by: str,
) -> ActionResult:
    """Mark an unmatched transfer (credit or debit) as not a client payment. Moves no money."""
    # every run of whitespace becomes one space, the ends are trimmed: that text is judged and stored
    reason = " ".join(reason.split())
    if not REASON_MIN_LEN <= len(reason) <= REASON_MAX_LEN or not reason.isprintable():
        raise ReconcileError(REASON_REFUSAL)
    async with conn.transaction():
        bank = await _lock_bank_row(conn, bank_txn_id)
        if bank["reconciled_status"] != "unmatched":
            raise ReconcileError(f"bank transaction {bank_txn_id} is not unmatched")

        await conn.execute(
            """
            UPDATE bank_transactions
            SET reconciled_status = 'ignored',
                matched_practice_id = NULL,
                matched_invoice_id = NULL
            WHERE id = $1
            """,
            bank_txn_id,
        )
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, confirmed_by, confirmed_at, amount_applied_idr, note)
            VALUES ($1, $2, now(), 0, $3)
            RETURNING id
            """,
            bank_txn_id,
            ignored_by,
            reason,
        )
    logger.info("Reconcile ignore: bank_txn=%s recon=%s", bank_txn_id, log_id)
    return ActionResult(bank_txn_id, "ignored", log_id)


async def reopen_transaction(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int,
    reopened_by: str,
) -> ActionResult:
    """Put a linked (`manual`) or `ignored` transfer back to `unmatched`.

    A `matched` row is never reopened here: money was recorded for it.
    """
    async with conn.transaction():
        bank = await _lock_bank_row(conn, bank_txn_id)
        if bank["reconciled_status"] not in ("manual", "ignored"):
            raise ReconcileError(f"bank transaction {bank_txn_id} is not manual or ignored")

        # Safe to read now: every writer of this bank transaction's log rows holds its lock.
        latest = await conn.fetchval(
            "SELECT id FROM reconciliation_log WHERE bank_txn_id = $1 ORDER BY id DESC LIMIT 1",
            bank_txn_id,
        )
        await conn.execute(
            """
            UPDATE bank_transactions
            SET reconciled_status = 'unmatched',
                matched_practice_id = NULL,
                matched_invoice_id = NULL
            WHERE id = $1
            """,
            bank_txn_id,
        )
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, confirmed_by, confirmed_at, amount_applied_idr,
                 reversal_of_id, note)
            VALUES ($1, $2, now(), 0, $3, $4)
            RETURNING id
            """,
            bank_txn_id,
            reopened_by,
            latest,
            REOPEN_NOTE,
        )
    logger.info("Reconcile reopen: bank_txn=%s recon=%s", bank_txn_id, log_id)
    return ActionResult(bank_txn_id, "unmatched", log_id)
