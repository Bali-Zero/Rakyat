-- ============================================================
-- 374_link_cashout_backfill.sql
-- Owner rule (e) for the links made before it. Since #553 a transfer linked to a payment the
-- team recorded by hand enters the cash-out and dates its invoice paid (link_recorded). The
-- owner decided (2026-11-21) that the transfers linked before #553 was served get the same,
-- once.
--
-- For each bank transaction in 'manual' with an invoice and no standing invoice_payment
-- cash-out row (linked_bank_txn_id = it, reversed_by_id IS NULL), one weekly_cashout row as
-- link_recorded writes it: the transfer's date, its ISO week ("IYYY-Www", as _iso_week_label),
-- its amount, the case, the invoice and the transfer, 'bank_transfer', recorded_by = who made
-- the link (the newest link row of the audit log) and recorded_at = when. Its description
-- marks it as added here. Then each invoice of such a row: paid_date = the later of the date
-- already there and the latest date of its rows added here (midnight UTC, as link_recorded
-- stores a statement date).
--
-- Not touched: practices, bank_transactions, reconciliation_log (immutable; reopen finds a
-- link's cash-out row by linked_bank_txn_id, not by the log), worksheet rows (planning
-- drafts, outside the cash-out totals). Consumer: cashout_service.list_cashout and
-- cashbook_summary (the cash-out view, its totals and the export of the accounting page) and
-- reconcile_service.reopen_transaction, which reverses such a row like any link's.
-- Date: 2026-11-21
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

INSERT INTO weekly_cashout
    (movement_date, week_label, direction, type, amount_idr,
     linked_practice_id, linked_invoice_id, linked_bank_txn_id,
     description, payment_method, recorded_by, recorded_at)
SELECT bt.txn_date,
       to_char(bt.txn_date, 'IYYY-"W"IW'),
       'in',
       'invoice_payment',
       bt.amount_idr,
       bt.matched_practice_id,
       bt.matched_invoice_id,
       bt.id,
       'older link, added to the cash-out by migration 374',
       'bank_transfer',
       COALESCE(link.confirmed_by, 'migration 374'),
       COALESCE(link.confirmed_at, now())
FROM bank_transactions bt
LEFT JOIN LATERAL (
    SELECT l.confirmed_by, l.confirmed_at
    FROM reconciliation_log l
    WHERE l.bank_txn_id = bt.id
      AND l.note LIKE 'linked to a payment already recorded%'
    ORDER BY l.id DESC
    LIMIT 1
) link ON true
WHERE bt.reconciled_status = 'manual'
  AND bt.matched_invoice_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM weekly_cashout wc
      WHERE wc.linked_bank_txn_id = bt.id
        AND wc.type = 'invoice_payment'
        AND wc.reversed_by_id IS NULL
  )
ORDER BY bt.id;

UPDATE invoices i
SET paid_date = GREATEST(i.paid_date, added.paid_date)
FROM (
    SELECT linked_invoice_id AS invoice_id,
           (max(movement_date))::timestamp AT TIME ZONE 'UTC' AS paid_date
    FROM weekly_cashout
    WHERE description = 'older link, added to the cash-out by migration 374'
      AND reversed_by_id IS NULL
    GROUP BY linked_invoice_id
) added
WHERE i.id = added.invoice_id;

-- === ROLLBACK ===
-- Reverses, as a reopen does, each row added above that nobody has reversed since: a copy
-- whose reversed_by_id is the row, then the row's reversed_by_id = the copy (a mutual pair,
-- hidden by every reader). Then each of their invoices: paid_date = the latest standing
-- invoice_payment date, or NULL.
WITH added AS (
    SELECT id
    FROM weekly_cashout
    WHERE description = 'older link, added to the cash-out by migration 374'
      AND reversed_by_id IS NULL
),
reversals AS (
    INSERT INTO weekly_cashout
        (movement_date, week_label, direction, type, amount_idr,
         linked_practice_id, linked_invoice_id, linked_bank_txn_id,
         description, payment_method, recorded_by, recorded_at, reversed_by_id)
    SELECT wc.movement_date, wc.week_label, wc.direction, wc.type, wc.amount_idr,
           wc.linked_practice_id, wc.linked_invoice_id, wc.linked_bank_txn_id,
           'reversal of cash-out row ' || wc.id || ': migration 374 rolled back',
           wc.payment_method, 'migration 374 rollback', now(), wc.id
    FROM weekly_cashout wc
    JOIN added ON added.id = wc.id
    RETURNING id, reversed_by_id AS original_id
)
UPDATE weekly_cashout wc
SET reversed_by_id = reversals.id
FROM reversals
WHERE wc.id = reversals.original_id;

UPDATE invoices i
SET paid_date = (
    SELECT (max(wc.movement_date))::timestamp AT TIME ZONE 'UTC'
    FROM weekly_cashout wc
    WHERE wc.linked_invoice_id = i.id
      AND wc.type = 'invoice_payment'
      AND wc.reversed_by_id IS NULL
)
WHERE i.id IN (
    SELECT linked_invoice_id
    FROM weekly_cashout
    WHERE description = 'older link, added to the cash-out by migration 374'
);
