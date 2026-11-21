-- ============================================================
-- 373_reconciliation_log_before_state.sql
-- Owner rule (b): a payment confirmed today can be undone, and the undo puts back what the
-- confirmation found. Two of those values are overwritten by the confirmation and kept
-- nowhere else: the invoice's paid date (the confirmation keeps the later of the old date and
-- the transfer's) and the case's start date (set only when it was empty, on a case the
-- confirmation started). before_state keeps them on the confirmation's own audit row, as
-- status_before already keeps the payment status.
-- Date: 2026-11-21
--
-- Shape written by confirm_payment:
--   {"invoice_paid_date": <ISO timestamptz or null>, "start_date": <ISO date or null>}
-- NULL on every row written before this migration and on rows that are not confirmations
-- (link, ignore, reopen, undo): the undo refuses a confirmation whose before_state is NULL
-- rather than guess what it overwrote.
--
-- Additive and nullable: the code before this migration never reads or writes the column.
-- The rollback refuses (see below), as 353's does.
--
-- Consumer: services/accounting/reconcile_service.py (confirm_payment writes it,
-- undo_confirmation reads it) through POST /api/crm/accounting/confirm and
-- POST /api/crm/accounting/undo-confirmation.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE reconciliation_log ADD COLUMN IF NOT EXISTS before_state JSONB;

-- === ROLLBACK ===
-- Additive and nullable: the code before 373 never reads or writes before_state, so the
-- routine rollback (revert the code) keeps the column and it goes quiet. Removing it would
-- make every confirmation of the day impossible to undo and restores no other invariant: it
-- is a separately authorized cleanup once nothing reads the column, not this rollback.
-- Refusing keeps both ledgers intact (migration_manager clears them only after the
-- rollback SQL succeeds), so a refused rollback never passes for a removal.
DO $$
BEGIN
  RAISE EXCEPTION 'rollback 373 refused: reconciliation_log.before_state is additive and nullable; revert the code and keep the column, remove it only as a separately authorized cleanup';
END $$;
