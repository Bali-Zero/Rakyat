-- Keep the original receipt and its replay result on the money-register row.
ALTER TABLE weekly_cashout
    ADD COLUMN IF NOT EXISTS cash_request_id UUID,
    ADD COLUMN IF NOT EXISTS cash_company_key TEXT,
    ADD COLUMN IF NOT EXISTS cash_balance_after_idr BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_cashout_cash_request
    ON weekly_cashout(cash_request_id) WHERE cash_request_id IS NOT NULL;

-- === ROLLBACK ===
DROP INDEX IF EXISTS uq_weekly_cashout_cash_request;
ALTER TABLE weekly_cashout DROP COLUMN IF EXISTS cash_request_id,
    DROP COLUMN IF EXISTS cash_company_key, DROP COLUMN IF EXISTS cash_balance_after_idr;
