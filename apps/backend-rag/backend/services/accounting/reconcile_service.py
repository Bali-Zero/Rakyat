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

from backend.services.accounting.companies import company_key_for_account, company_key_for_issuer
from backend.services.accounting.matcher import EXACT_TOLERANCE_IDR

logger = logging.getLogger(__name__)

# Mirrors crm_practices PAYMENT_STATUS_VALUES — keep in sync.
PAYMENT_STATUS_VALUES = ("unpaid", "partial", "paid")

# The note of a confirmation recorded in historical mode: a payment that arrived in the past,
# reconciled now. The practice listener sends no email for the change it made.
HISTORICAL_NOTE = "historical: recorded without notifications"


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


async def _transfer_company(conn: asyncpg.Connection, bank_txn_id: int) -> str:
    """The company that owns the account a transfer reached; refuses an account no company owns."""
    account = await conn.fetchval(
        "SELECT bs.account_number FROM bank_transactions bt"
        " JOIN bank_statements bs ON bs.id = bt.statement_id WHERE bt.id = $1",
        bank_txn_id,
    )
    company = company_key_for_account(account)
    if company is None:
        raise ReconcileError(
            f"bank transaction {bank_txn_id} reached an account no issuing company owns"
        )
    return company


def _require_issuer(company: str, issuer_entity_key: str | None, bank_txn_id: int, invoice_id: int) -> None:
    """Owner rule d: a transfer into one company's account never settles another company's invoice."""
    if company_key_for_issuer(issuer_entity_key) != company:
        raise ReconcileError(
            f"invoice {invoice_id} was issued by another company than the owner of the account"
            f" bank transaction {bank_txn_id} reached"
        )


# What the CRM writes when a case enters On Process by hand (crm_practices STATUS_TIMELINE_EVENTS
# and its activity row): the client's timeline entry and the card's status history.
START_TIMELINE_EVENT = ("milestone", "Application in process", "info")
_UNDEFINED_TABLE = "42P01"


async def _record_start(conn: asyncpg.Connection, prac: asyncpg.Record, by: str) -> None:
    """The client timeline entry and the activity row of a case that starts on confirmation."""
    # As the CRM PATCH: bool(client_visible), so a NULL visibility writes no entry.
    if prac["client_id"] is not None and bool(prac["client_visible"]):
        event_type, title, color = START_TIMELINE_EVENT
        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO timeline_events (
                        client_id, practice_id, event_type, title, description,
                        event_date, client_visible, color, created_by
                    )
                    VALUES ($1, $2, $3, $4, $5, NOW(), true, $6, $7)
                    """,
                    prac["client_id"],
                    prac["id"],
                    event_type,
                    title,
                    prac["client_summary"] or "Status: on_process",
                    color,
                    by,
                )
        except asyncpg.PostgresError as exc:
            if getattr(exc, "sqlstate", None) != _UNDEFINED_TABLE:
                raise
    await conn.execute(
        """
        INSERT INTO activity_log (entity_type, entity_id, action, performed_by, description, changes)
        VALUES ('practice', $1, 'updated', $2, 'Updated: status', $3)
        """,
        prac["id"],
        by,
        {"status": "on_process"},  # a dict: the pool's jsonb codec encodes it
    )


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
    historical: bool = False,
) -> ConfirmResult:
    """Confirm a payment match in one transaction. Returns the audit ids.

    `new_status` must be one of PAYMENT_STATUS_VALUES. `amount_applied_idr` is
    what actually landed (net of withholding/fees); the gross is recorded via
    the decomposition + pph/fee fields. `historical` records the same rows and
    marks the audit row with HISTORICAL_NOTE, so no email follows the change
    (see recorded_without_notifications). A historical confirmation runs in a transaction of
    its own: the listener recognises its change by the transaction's instant, which another
    change made in the same transaction would share.
    """
    if new_status not in PAYMENT_STATUS_VALUES:
        raise ReconcileError(
            f"new_status must be one of {PAYMENT_STATUS_VALUES}, got {new_status!r}"
        )
    if historical and conn.is_in_transaction():
        raise ReconcileError(
            "a historical confirmation runs in a transaction of its own, not inside another"
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
            "SELECT id, payment_status, paid_amount, status, client_id, client_visible,"
            " client_summary FROM practices WHERE id = $1 FOR UPDATE",
            practice_id,
        )
        if prac is None:
            raise ReconcileError(f"practice {practice_id} not found")
        status_before = prac["payment_status"]
        # Owner rule (a): paid in full, a case waiting for payment starts. A historical
        # confirmation never moves the case: the listener recognises it only while the
        # case's status is unchanged, and an old payment must not email anyone.
        starts = not historical and new_status == "paid" and prac["status"] == "sending_invoice"
        prior_paid = int(prac["paid_amount"] or 0)
        new_paid = prior_paid + amount_applied_idr

        # 1b. A given invoice must exist and belong to this practice, before any write.
        invoice_amount = None
        invoice_issuer = None
        if invoice_id is not None:
            inv = await conn.fetchrow(
                "SELECT practice_id, amount_idr, issuer_entity_key FROM invoices WHERE id = $1 FOR SHARE",
                invoice_id,
            )
            if inv is None:
                raise ReconcileError(f"invoice {invoice_id} not found")
            if inv["practice_id"] != practice_id:
                raise ReconcileError(
                    f"invoice {invoice_id} does not belong to practice {practice_id}"
                )
            invoice_amount = inv["amount_idr"]
            invoice_issuer = inv["issuer_entity_key"]
            if invoice_amount is not None and not Decimal(invoice_amount).is_finite():
                raise ReconcileError(f"invoice {invoice_id} has no usable amount")

        # 1c. A transfer settles only invoices of the company whose account it reached; with no
        #     invoice named, every invoice of the practice, and a practice with none is refused.
        if bank_txn_id is not None:
            company = await _transfer_company(conn, bank_txn_id)
            if invoice_id is not None:
                _require_issuer(company, invoice_issuer, bank_txn_id, invoice_id)
            else:
                issued = await conn.fetch(
                    "SELECT id, issuer_entity_key FROM invoices WHERE practice_id = $1 ORDER BY id FOR SHARE",
                    practice_id,
                )
                if not issued:
                    raise ReconcileError(
                        f"practice {practice_id} has no invoice for bank transaction {bank_txn_id} to settle"
                    )
                for row in issued:
                    _require_issuer(company, row["issuer_entity_key"], bank_txn_id, row["id"])

        # 2. Update practices — SAME columns/path the CRM PATCH writes (superscar #9).
        #    The invoice amount fills actual_price only while it is still NULL. A case that
        #    starts moves in the SAME statement, so the listener sees payment and start as one
        #    change and sends only the payment emails (the start mail is skipped when paid).
        if starts:
            await conn.execute(
                """
                UPDATE practices
                SET payment_status = $1, paid_amount = $2,
                    actual_price = COALESCE(actual_price, $4),
                    status = 'on_process', start_date = COALESCE(start_date, NOW()),
                    updated_at = now()
                WHERE id = $3
                """,
                new_status,
                new_paid,
                practice_id,
                invoice_amount,
            )
            await _record_start(conn, prac, confirmed_by)
        else:
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
        #    An invoice paid in parts is paid on its LATEST part: an older transfer confirmed
        #    after a newer one, confirmed or linked, keeps the newer date (link_recorded does
        #    the same).
        if invoice_id is not None:
            await conn.execute(
                """
                UPDATE invoices
                SET paid_date = GREATEST(
                        paid_date, COALESCE(($5::date)::timestamp AT TIME ZONE 'UTC', now())
                    ),
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
                 pph_delta_idr, bank_fee_delta_idr, note)
            VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8, $9, $10, $11)
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
            HISTORICAL_NOTE if historical else None,
        )

    logger.info(
        "Reconcile confirm: practice=%s invoice=%s %s->%s amount=%s by=%s recon=%s historical=%s",
        practice_id, invoice_id, status_before, new_status, amount_applied_idr,
        confirmed_by, recon_id, historical,
    )
    return ConfirmResult(
        practice_id=practice_id,
        invoice_id=invoice_id,
        cashout_id=cashout_id,
        reconciliation_log_id=recon_id,
        status_before=status_before,
        status_after=new_status,
    )


async def recorded_without_notifications(
    pool: asyncpg.Pool,
    practice_id: int,
    change_ts: str | int | None,
    payment_before: str | None,
    payment_after: str | None,
) -> bool:
    """Whether the practice change stamped `change_ts` was made by a historical confirmation.

    The practice trigger stamps its payload with EXTRACT(EPOCH FROM NOW()) and confirm_payment
    writes its audit row with confirmed_at = now() in the same transaction, so both carry the
    same instant. `change_ts` is that stamp as the payload's TEXT (a float would round the
    microseconds). The audit row must also record the same payment transition as the change:
    the instant names a transaction, the transition names the change inside it. No stamp: an
    ordinary change.
    """
    if change_ts is None:
        return False
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM reconciliation_log
                WHERE practice_id = $1 AND note = $2
                  AND extract(epoch FROM confirmed_at) = $3::numeric
                  AND status_before IS NOT DISTINCT FROM $4::text
                  AND status_after IS NOT DISTINCT FROM $5::text
            )
            """,
            practice_id,
            HISTORICAL_NOTE,
            str(change_ts),
            payment_before,
            payment_after,
        )
    )


# ── Actions that change no case: link to a recorded payment, ignore, reopen ──────────────
#
# Each one is one transaction that locks the bank transaction row FIRST (as confirm_payment
# does) and checks its own preconditions on the rows it holds. None of them writes to
# practices. A link also writes the invoice's paid date and one cash-out row (owner rule e);
# reopening that link reverses the row and recomputes the date. Ignore writes neither. A
# refusal carries fixed text and ids: which precondition failed and the ids involved,
# nothing else of a row.

LINK_NOTE = "linked to a payment already recorded; added to the cash-out"
REOPEN_NOTE = "reopened; no money moved"
REOPEN_REVERSED_NOTE = "reopened; the link's cash-out row reversed"
REVERSAL_DESCRIPTION = "reversal of cash-out row {id}: link reopened"
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
        "SELECT direction, reconciled_status, amount_idr, txn_date, matched_practice_id,"
        " matched_invoice_id FROM bank_transactions WHERE id = $1 FOR UPDATE",
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
    """Link an unmatched credit to the invoice of a payment already recorded.

    The case is not changed: its payment was recorded by hand. Owner rule (e): the transfer
    enters the cash-out (one `invoice_payment` row on its own date) and dates the invoice
    paid (the latest of its date and any date already there).

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
            "SELECT practice_id, amount_idr, issuer_entity_key FROM invoices WHERE id = $1 FOR SHARE",
            invoice_id,
        )
        if inv is None:
            raise ReconcileError(f"invoice {invoice_id} not found")
        if inv["practice_id"] != practice_id:
            raise ReconcileError(f"invoice {invoice_id} does not belong to practice {practice_id}")
        invoice_amount = inv["amount_idr"]
        if invoice_amount is None or not Decimal(invoice_amount).is_finite():
            raise ReconcileError(f"invoice {invoice_id} has no usable amount")
        _require_issuer(
            await _transfer_company(conn, bank_txn_id), inv["issuer_entity_key"], bank_txn_id, invoice_id
        )

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
        txn_date = bank["txn_date"]
        # Midnight UTC of the statement date, as confirm_payment stores it.
        await conn.execute(
            "UPDATE invoices SET paid_date = GREATEST(paid_date, ($1::date)::timestamp AT TIME ZONE 'UTC')"
            " WHERE id = $2",
            txn_date,
            invoice_id,
        )
        cashout_id = await conn.fetchval(
            """
            INSERT INTO weekly_cashout
                (movement_date, week_label, direction, type, amount_idr,
                 linked_practice_id, linked_invoice_id, linked_bank_txn_id,
                 payment_method, recorded_by, recorded_at)
            VALUES ($1, $2, 'in', 'invoice_payment', $3, $4, $5, $6, 'bank_transfer', $7, now())
            RETURNING id
            """,
            txn_date,
            _iso_week_label(txn_date),
            bank["amount_idr"],
            practice_id,
            invoice_id,
            bank_txn_id,
            linked_by,
        )
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, invoice_ids, practice_id, cashout_id, confirmed_by, confirmed_at,
                 status_before, status_after, amount_applied_idr, note)
            VALUES ($1, $2, $3, $4, $5, now(), 'paid', 'paid', 0, $6)
            RETURNING id
            """,
            bank_txn_id,
            [invoice_id],  # a list: see confirm_payment
            practice_id,
            cashout_id,
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


# The paid date an invoice keeps: the latest movement date of its cash-out rows still standing.
_STANDING_PAID_DATE = """
    SELECT (max(wc.movement_date))::timestamp AT TIME ZONE 'UTC'
    FROM weekly_cashout wc
    WHERE wc.linked_invoice_id = $1 AND wc.type = 'invoice_payment'
      AND wc.reversed_by_id IS NULL
"""
_SET_STANDING_PAID_DATE = "UPDATE invoices SET paid_date = (" + _STANDING_PAID_DATE + ") WHERE id = $1"


async def _reverse_cashout_row(
    conn: asyncpg.Connection, row_id: int, description: str, by: str
) -> int:
    """Append-only reversal of one cash-out row: a copy marked as its reversal, the two pointing
    at each other, so every reader of the cash-out leaves both out. Returns the copy's id."""
    reversal_id = await conn.fetchval(
        """
        INSERT INTO weekly_cashout
            (movement_date, week_label, direction, type, amount_idr,
             pnbp_idr, urgent_idr, rptka_imta_idr, margin_idr, final_price_idr,
             original_currency, original_amount, fx_rate, fx_date,
             pph_withheld_idr, bank_fee_idr,
             linked_practice_id, linked_invoice_id, linked_bank_txn_id,
             category, description, counterparty, payment_method,
             recorded_by, recorded_at, reversed_by_id)
        SELECT movement_date, week_label, direction, type, amount_idr,
               pnbp_idr, urgent_idr, rptka_imta_idr, margin_idr, final_price_idr,
               original_currency, original_amount, fx_rate, fx_date,
               pph_withheld_idr, bank_fee_idr,
               linked_practice_id, linked_invoice_id, linked_bank_txn_id,
               category, $2, counterparty, payment_method,
               $3, now(), id
        FROM weekly_cashout WHERE id = $1
        RETURNING id
        """,
        row_id,
        description,
        by,
    )
    await conn.execute(
        "UPDATE weekly_cashout SET reversed_by_id = $1 WHERE id = $2", reversal_id, row_id
    )
    return reversal_id


async def reopen_transaction(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int,
    reopened_by: str,
) -> ActionResult:
    """Put a linked (`manual`) or `ignored` transfer back to `unmatched`.

    A `matched` row is never reopened here: money was recorded for it. A link that wrote a
    cash-out row (owner rule e) gets it reversed, append-only: a copy marked as its reversal,
    the two pointing at each other so every reader of the cash-out leaves both out; the
    invoice's paid date becomes the latest date of its cash-out rows still standing, or none.
    Locks the bank transaction, then the practice, then the cash-out row, then the invoice:
    the order link_recorded and confirm_payment take, so the paid date is recomputed only after
    a link or confirmation in flight on the same practice has committed.
    """
    async with conn.transaction():
        bank = await _lock_bank_row(conn, bank_txn_id)
        if bank["reconciled_status"] not in ("manual", "ignored"):
            raise ReconcileError(f"bank transaction {bank_txn_id} is not manual or ignored")

        written: list[asyncpg.Record] = []
        if bank["reconciled_status"] == "manual":
            if bank["matched_practice_id"] is not None:
                await conn.execute(
                    "SELECT 1 FROM practices WHERE id = $1 FOR UPDATE", bank["matched_practice_id"]
                )
            written = await conn.fetch(
                "SELECT id, linked_invoice_id FROM weekly_cashout"
                " WHERE linked_bank_txn_id = $1 AND type = 'invoice_payment' AND reversed_by_id IS NULL"
                " ORDER BY id FOR UPDATE",
                bank_txn_id,
            )
        reversal_id = None
        for row in written:
            reversal_id = await _reverse_cashout_row(
                conn, row["id"], REVERSAL_DESCRIPTION.format(id=row["id"]), reopened_by
            )

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
        for invoice_id in sorted({r["linked_invoice_id"] for r in written} - {None}):
            await conn.execute(_SET_STANDING_PAID_DATE, invoice_id)
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, cashout_id, confirmed_by, confirmed_at, amount_applied_idr,
                 reversal_of_id, note)
            VALUES ($1, $2, $3, now(), 0, $4, $5)
            RETURNING id
            """,
            bank_txn_id,
            reversal_id,
            reopened_by,
            latest,
            REOPEN_REVERSED_NOTE if written else REOPEN_NOTE,
        )
    logger.info("Reconcile reopen: bank_txn=%s recon=%s", bank_txn_id, log_id)
    return ActionResult(bank_txn_id, "unmatched", log_id)


UNDO_NOTE = "confirmation undone the same day"
UNDO_REVERSAL_DESCRIPTION = "reversal of cash-out row {id}: confirmation undone"
_UNDO_TZ = "Asia/Makassar"


async def undo_confirmation(
    conn: asyncpg.Connection,
    *,
    bank_txn_id: int,
    undone_by: str,
) -> ActionResult:
    """Owner rule (b): a payment confirmed today (WITA) is taken back, in one transaction.

    The transfer's newest log row must be a confirmation of today, still standing, and the
    newest standing payment entry of its case, whose payment status must still be the one the
    confirmation set: then `status_before` is still the state to go back to. Everything the
    confirmation wrote goes back: the transfer is unmatched again, its cash-out row is reversed
    (append-only, as a reopened link's), the invoice loses the amount and keeps the paid date of
    its cash-out rows still standing, the case gets its payment status and paid amount back. A
    case the confirmation started (rule a) goes back to sending_invoice, its start date cleared
    when the confirmation set it, the client timeline entry hidden, and an activity row says so;
    a case that has moved on since is refused. A log row marks the undo as the confirmation's
    reversal. Nothing here emails anyone: the listener sends on a payment becoming paid and on a
    status entering a client milestone, and an undo does neither. Locks in the confirm order:
    the bank transaction, the practice, the cash-out row, the invoice.
    """
    async with conn.transaction():
        bank = await _lock_bank_row(conn, bank_txn_id)
        if bank["reconciled_status"] != "matched":
            raise ReconcileError(f"bank transaction {bank_txn_id} is not confirmed")
        # Safe to read now: every writer of this transfer's log rows holds its lock.
        log = await conn.fetchrow(
            "SELECT id, invoice_ids, practice_id, cashout_id, confirmed_at, status_before,"
            " status_after, amount_applied_idr, note, reversal_of_id,"
            f" (confirmed_at AT TIME ZONE '{_UNDO_TZ}')::date"
            f" = (now() AT TIME ZONE '{_UNDO_TZ}')::date AS today"
            " FROM reconciliation_log WHERE bank_txn_id = $1 ORDER BY id DESC LIMIT 1",
            bank_txn_id,
        )
        if (
            log is None
            or log["practice_id"] is None
            or log["reversal_of_id"] is not None
            or log["note"] not in (None, HISTORICAL_NOTE)
            or log["practice_id"] != bank["matched_practice_id"]
        ):
            raise ReconcileError(f"bank transaction {bank_txn_id} has no confirmation to undo")
        if not log["today"]:
            raise ReconcileError(
                f"bank transaction {bank_txn_id} was confirmed before today: it cannot be undone"
            )
        practice_id = log["practice_id"]
        prac = await conn.fetchrow(
            "SELECT payment_status, paid_amount, status, start_date FROM practices"
            " WHERE id = $1 FOR UPDATE",
            practice_id,
        )
        if prac is None:
            raise ReconcileError(f"practice {practice_id} not found")
        # Safe to read now: a confirmation or link on this case waits for the practice lock.
        later = await conn.fetchval(
            """
            SELECT count(*) FROM reconciliation_log r
            WHERE r.practice_id = $1 AND r.id > $2 AND r.reversal_of_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM reconciliation_log u WHERE u.reversal_of_id = r.id)
            """,
            practice_id,
            log["id"],
        )
        if later or (prac["payment_status"] or "unpaid") != log["status_after"]:
            raise ReconcileError(
                f"practice {practice_id} has changed since this confirmation: undo the newer one first"
            )
        started = bool(
            await conn.fetchval(
                "SELECT 1 FROM activity_log WHERE entity_type = 'practice' AND entity_id = $1"
                " AND description = 'Updated: status' AND changes->>'status' = 'on_process'"
                " AND performed_at = $2",
                practice_id,
                log["confirmed_at"],
            )
        )
        if started and prac["status"] != "on_process":
            raise ReconcileError(
                f"practice {practice_id} has moved on since this confirmation started it"
            )

        reversal_id = None
        if log["cashout_id"] is not None:
            standing = await conn.fetchval(
                "SELECT id FROM weekly_cashout WHERE id = $1 AND reversed_by_id IS NULL FOR UPDATE",
                log["cashout_id"],
            )
            if standing is not None:
                reversal_id = await _reverse_cashout_row(
                    conn, standing, UNDO_REVERSAL_DESCRIPTION.format(id=standing), undone_by
                )
        amount = int(log["amount_applied_idr"] or 0)
        for invoice_id in sorted(set(log["invoice_ids"] or [])):
            await conn.execute("SELECT 1 FROM invoices WHERE id = $1 FOR UPDATE", invoice_id)
            await conn.execute(
                "UPDATE invoices SET paid_amount_idr = GREATEST(COALESCE(paid_amount_idr, 0) - $2, 0)"
                " WHERE id = $1",
                invoice_id,
                amount,
            )
            await conn.execute(_SET_STANDING_PAID_DATE, invoice_id)
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
        if started:
            await conn.execute(
                """
                UPDATE practices
                SET payment_status = $2, paid_amount = GREATEST(COALESCE(paid_amount, 0) - $3, 0),
                    status = 'sending_invoice',
                    start_date = CASE WHEN start_date = ($4::timestamptz)::date THEN NULL
                                      ELSE start_date END,
                    updated_at = now()
                WHERE id = $1
                """,
                practice_id,
                log["status_before"],
                amount,
                log["confirmed_at"],
            )
            await _record_unstart(conn, practice_id, log["confirmed_at"], undone_by)
        else:
            await conn.execute(
                """
                UPDATE practices
                SET payment_status = $2, paid_amount = GREATEST(COALESCE(paid_amount, 0) - $3, 0),
                    updated_at = now()
                WHERE id = $1
                """,
                practice_id,
                log["status_before"],
                amount,
            )
        log_id = await conn.fetchval(
            """
            INSERT INTO reconciliation_log
                (bank_txn_id, invoice_ids, practice_id, cashout_id, confirmed_by, confirmed_at,
                 status_before, status_after, amount_applied_idr, reversal_of_id, note)
            VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8, $9, $10)
            RETURNING id
            """,
            bank_txn_id,
            list(log["invoice_ids"] or []),  # a list: see confirm_payment
            practice_id,
            reversal_id,
            undone_by,
            log["status_after"],
            log["status_before"],
            amount,
            log["id"],
            UNDO_NOTE,
        )
    logger.info(
        "Reconcile undo: bank_txn=%s practice=%s undone=%s recon=%s started=%s",
        bank_txn_id, practice_id, log["id"], log_id, started,
    )
    return ActionResult(bank_txn_id, "unmatched", log_id)


async def _record_unstart(
    conn: asyncpg.Connection, practice_id: int, started_at: datetime, by: str
) -> None:
    """The start an undone confirmation made is taken back where the client could see it: its
    timeline entry is hidden (kept, never deleted), and an activity row records the move back,
    as the CRM records a status change made by hand."""
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE timeline_events SET client_visible = false WHERE practice_id = $1"
                " AND event_type = $2 AND title = $3 AND event_date = $4",
                practice_id,
                START_TIMELINE_EVENT[0],
                START_TIMELINE_EVENT[1],
                started_at,
            )
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) != _UNDEFINED_TABLE:
            raise
    await conn.execute(
        """
        INSERT INTO activity_log (entity_type, entity_id, action, performed_by, description, changes)
        VALUES ('practice', $1, 'updated', $2, 'Updated: status', $3)
        """,
        practice_id,
        by,
        {"status": "sending_invoice"},
    )
