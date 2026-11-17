-- ============================================================
-- 363_bank_statement_import.sql
-- Storage for an imported bank statement (CIMB OCTO Biz CSV). Migration 238 created
-- bank_statements / bank_transactions and nothing has written to them; this adds the
-- columns the import needs (exact timestamps and NUMERIC(18,2) amounts, the account
-- number, the row kind, the history-segment count) and the unique identity index that
-- keeps a transaction from being stored twice.
-- Date: 2026-11-17
--
-- Additive only: nullable or defaulted columns and indexes, all IF NOT EXISTS. No
-- existing column or constraint changes, so rows written under 238 alone stay valid.
-- Run twice it does nothing the second time. The runtime role already holds the
-- table-level grants of 238, which cover new columns: none are added here.
--
-- Consumer: services/accounting/bank_import_service.py through
-- POST /api/crm/accounting/bank-statement/import.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE bank_statements
    ADD COLUMN IF NOT EXISTS account_number   TEXT,
    ADD COLUMN IF NOT EXISTS account_name     TEXT,
    ADD COLUMN IF NOT EXISTS period_first_at  TIMESTAMP,
    ADD COLUMN IF NOT EXISTS period_last_at   TIMESTAMP,
    ADD COLUMN IF NOT EXISTS opening_balance  NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS closing_balance  NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS order_decided_by TEXT,
    ADD COLUMN IF NOT EXISTS history_segments INTEGER,
    ADD COLUMN IF NOT EXISTS rows_inserted    INTEGER,
    ADD COLUMN IF NOT EXISTS rows_duplicate   INTEGER;

ALTER TABLE bank_transactions
    ADD COLUMN IF NOT EXISTS posted_at   TIMESTAMP,
    ADD COLUMN IF NOT EXISTS debit       NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS credit      NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS balance     NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS kind        TEXT,
    ADD COLUMN IF NOT EXISTS dup_ordinal INTEGER NOT NULL DEFAULT 0;

-- A transaction's identity: account, instant, both amounts, balance, and its ordinal
-- among identical rows of one statement (zero-amount rows can repeat exactly).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_txn_identity
    ON bank_transactions (bank_account_id, posted_at, debit, credit, balance, dup_ordinal)
    WHERE posted_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bank_txn_account_posted
    ON bank_transactions (bank_account_id, posted_at);

-- === ROLLBACK ===

DROP INDEX IF EXISTS idx_bank_txn_account_posted;
DROP INDEX IF EXISTS uq_bank_txn_identity;
ALTER TABLE bank_transactions
    DROP COLUMN IF EXISTS dup_ordinal,
    DROP COLUMN IF EXISTS kind,
    DROP COLUMN IF EXISTS balance,
    DROP COLUMN IF EXISTS credit,
    DROP COLUMN IF EXISTS debit,
    DROP COLUMN IF EXISTS posted_at;
ALTER TABLE bank_statements
    DROP COLUMN IF EXISTS rows_duplicate,
    DROP COLUMN IF EXISTS rows_inserted,
    DROP COLUMN IF EXISTS history_segments,
    DROP COLUMN IF EXISTS order_decided_by,
    DROP COLUMN IF EXISTS closing_balance,
    DROP COLUMN IF EXISTS opening_balance,
    DROP COLUMN IF EXISTS period_last_at,
    DROP COLUMN IF EXISTS period_first_at,
    DROP COLUMN IF EXISTS account_name,
    DROP COLUMN IF EXISTS account_number;
