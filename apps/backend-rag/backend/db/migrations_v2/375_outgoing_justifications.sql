-- ============================================================
-- 375_outgoing_justifications.sql
-- The accountant justifies each bank debit (owner 2026-11-21/22). An APPEND-ONLY log: a
-- justification is never edited or deleted, it is undone (undone_by / undone_at) and a later
-- row justifies again. At most one ACTIVE (not undone) justification per debit, enforced by a
-- partial unique index. A cash withdrawal carries its breakdown in outgoing_justification_lines
-- (the application checks that the lines sum exactly to the debit before it writes them).
-- Date: 2026-11-22
--
-- Owner decision 2026-11-22: a justified debit NEVER becomes a weekly_cashout row; nothing
-- here touches that table. practice_id is optional for every category.
-- company_key is the issuing company of the statement's account, kept on the row so the
-- per-company register reads it without a join.
--
-- Consumer: services/accounting/outgoing_justify.py through
-- POST /api/crm/accounting/outgoings/{txn_id}/justify, .../undo and GET .../outgoings.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS outgoing_justifications (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txn_id        BIGINT NOT NULL REFERENCES bank_transactions(id),
    company_key   TEXT NOT NULL,
    category      TEXT NOT NULL CHECK (category IN (
                      'pnbp_case', 'vendor_case', 'staff_reimbursement', 'payroll',
                      'cash_withdrawal', 'bank_charges', 'tax', 'own_transfer', 'other')),
    practice_id   INTEGER REFERENCES practices(id) ON DELETE SET NULL,
    recipient     TEXT,
    note          TEXT,
    justified_by  TEXT NOT NULL CHECK (char_length(justified_by) BETWEEN 3 AND 254),
    justified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_by     TEXT,
    undone_at     TIMESTAMPTZ,
    CONSTRAINT outgoing_justifications_undone_ck CHECK ((undone_by IS NULL) = (undone_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_outgoing_justifications_active
    ON outgoing_justifications (txn_id) WHERE undone_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_outgoing_justifications_txn
    ON outgoing_justifications (txn_id);

CREATE TABLE IF NOT EXISTS outgoing_justification_lines (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    justification_id  BIGINT NOT NULL REFERENCES outgoing_justifications(id),
    amount_idr        BIGINT NOT NULL CHECK (amount_idr > 0),
    category          TEXT NOT NULL CHECK (category IN (
                          'pnbp_case', 'vendor_case', 'staff_reimbursement', 'payroll',
                          'bank_charges', 'tax', 'own_transfer', 'other')),
    note              TEXT NOT NULL CHECK (btrim(note) <> ''),
    practice_id       INTEGER REFERENCES practices(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_outgoing_justification_lines_parent
    ON outgoing_justification_lines (justification_id);

COMMENT ON TABLE outgoing_justifications IS
    'Append-only log of the accountant''s justification of each bank debit; undone, never deleted. One active row per debit.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS outgoing_justification_lines;
DROP TABLE IF EXISTS outgoing_justifications;
