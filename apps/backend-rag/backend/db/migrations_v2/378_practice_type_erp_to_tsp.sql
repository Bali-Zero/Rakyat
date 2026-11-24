-- Migration 378: show the other_erp practice type as TSP (Termination Stay Permit)
--
-- Decision DAI-176: the service recorded as "ERP" is the termination of a stay
-- permit filed from abroad, so its display name becomes
-- "TSP (Termination Stay Permit)". It is the same service at the same price.
-- practice_types.name is what invoices and the CRM print.
--
-- Only the name changes. The code stays other_erp, because practices and the
-- invoice issuer map refer to it. base_price, description and is_active are
-- not touched. The canonical sheet
-- (backend/data/bali_zero_official_prices_2026.json) carries the same name, so
-- the row keeps reconciling against the sheet by name.
--
-- Numbered 378: main's highest file at writing is 377, and no open pull
-- request carries 378.
--
-- Idempotent: the predicate no longer matches after the first run. A database
-- without the row updates nothing and passes the postcondition.

UPDATE practice_types
SET name = 'TSP (Termination Stay Permit)',
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'other_erp'
  AND name LIKE 'ERP%';

-- Postcondition: refuse to record this migration as applied while the row
-- still carries an ERP name.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM practice_types
        WHERE code = 'other_erp'
          AND name LIKE 'ERP%'
    ) THEN
        RAISE EXCEPTION 'migration 378: practice type other_erp is still named ERP';
    END IF;
END $$;

-- === ROLLBACK ===
UPDATE practice_types
SET name = 'ERP (Exit Re-entry Permit)',
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'other_erp'
  AND name = 'TSP (Termination Stay Permit)';
