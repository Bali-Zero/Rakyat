-- ============================================================
-- 376_outgoing_justification_batch.sql
-- «Justify all PNBP» (owner 2026-11-22): the accountant justifies the listed PNBP debits in one
-- go instead of one click each (245 of them on 2026-11-21). Every debit still gets its own row
-- in outgoing_justifications; the rows written together share batch_id, so the batch can be
-- taken back together on the day it was made. NULL on every row justified one at a time.
--
-- Additive and nullable: the code before this migration never reads or writes the column.
-- The rollback refuses (see below), as 373's does.
--
-- Consumer: services/accounting/outgoing_justify.py (justify_pnbp_batch writes it, undo_batch
-- reads it) through POST /api/crm/accounting/outgoings/pnbp-batch and
-- POST /api/crm/accounting/outgoings/batches/{batch_id}/undo; GET .../outgoings shows it.
-- Date: 2026-11-22
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE outgoing_justifications ADD COLUMN IF NOT EXISTS batch_id UUID;

-- === ROLLBACK ===
-- Additive and nullable: the code before 376 never reads or writes batch_id, so the routine
-- rollback (revert the code) keeps the column and it goes quiet. Removing it would make a batch
-- justified today impossible to undo as a batch and restores no other invariant: it is a
-- separately authorized cleanup once nothing reads the column, not this rollback.
DO $$
BEGIN
  RAISE EXCEPTION 'rollback 376 refused: outgoing_justifications.batch_id is additive and nullable; revert the code and keep the column, remove it only as a separately authorized cleanup';
END $$;
