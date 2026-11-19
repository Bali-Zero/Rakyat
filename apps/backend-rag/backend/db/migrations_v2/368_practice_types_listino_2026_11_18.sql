-- Migration 368: CRM default prices follow the 2026-11-18 price-list change
--
-- The canonical sheet (backend/data/bali_zero_official_prices_2026.json) moved
-- on 2026-11-18:
--
--   "Working KITAS (Altus/Onshore)"  36.000.000 -> 36.500.000 IDR
--   "Dissolving Company" (new row)   13.000.000 IDR
--
-- `practice_types.base_price` is what crm_practices.py quotes when a practice is
-- opened without an explicit price, and what invoice_service.py bills, so the
-- table has to say the same as the sheet:
--
--   practice_types.code = 'kitas_working_onshore'  base_price -> 36500000
--   practice_types.code = 'company_dissolving'     base_price -> 13000000
--
-- The second row already sells at 13.000.000 (migration 220); it is asserted
-- here so the table and the new sheet row are tied together by a statement,
-- not by coincidence. On a database that already holds 13000000 it updates
-- zero rows.
--
-- Deliberately NOT touched: every other practice type, including the VOA codes
-- (the sheet's VOA row is unchanged by this release).
--
-- Idempotent: code-scoped and value-guarded, so a rerun updates zero rows
-- rather than churning updated_at.

-- kitas_working_onshore was seeded by a one-off script, not by a SQL migration:
-- a database built from the migrations alone does not have the row, while
-- production does. Seed it when absent, with the values the production row
-- carries, so that the price change and the postcondition below mean the same
-- thing on every database. Where the row exists this inserts nothing and
-- changes nothing (company_dissolving needs no seed: migration 220 inserts it).
INSERT INTO practice_types (
    code, name, description, category, base_price,
    typical_duration_days, is_active
)
VALUES (
    'kitas_working_onshore',
    'Working KITAS (Altus/Onshore)',
    NULL,
    'kitas',
    36500000,
    365,
    true
)
ON CONFLICT (code) DO NOTHING;

UPDATE practice_types
   SET base_price = 36500000,
       updated_at = CURRENT_TIMESTAMP
 WHERE code = 'kitas_working_onshore'
   AND base_price IS DISTINCT FROM 36500000;

UPDATE practice_types
   SET base_price = 13000000,
       updated_at = CURRENT_TIMESTAMP
 WHERE code = 'company_dissolving'
   AND base_price IS DISTINCT FROM 13000000;

-- Postcondition: refuse to record this migration as applied unless both rows
-- exist at the ruled price. A silent no-op would leave the CRM quoting the old
-- figure while the migration log claimed the change had landed.
DO $$
DECLARE
    onshore numeric;
    dissolving numeric;
BEGIN
    SELECT base_price INTO onshore FROM practice_types WHERE code = 'kitas_working_onshore';
    SELECT base_price INTO dissolving FROM practice_types WHERE code = 'company_dissolving';

    IF onshore IS DISTINCT FROM 36500000 THEN
        RAISE EXCEPTION
            'migration 368: practice_types.kitas_working_onshore is %, expected 36500000',
            onshore;
    END IF;

    IF dissolving IS DISTINCT FROM 13000000 THEN
        RAISE EXCEPTION
            'migration 368: practice_types.company_dissolving is %, expected 13000000',
            dissolving;
    END IF;
END;
$$;

-- === ROLLBACK ===

-- Restores the superseded onshore figure. company_dissolving is left alone: it
-- was already 13000000 before this migration (migration 220). A row this
-- migration seeded on a database that lacked it stays, at the superseded price.
UPDATE practice_types
   SET base_price = 36000000,
       updated_at = CURRENT_TIMESTAMP
 WHERE code = 'kitas_working_onshore';
