-- Migration 379: put the Driving License service back on sale
--
-- Ruled by the owner on 2026-11-23 (decision K135): Driving License is a
-- service again, invoiced by PT Bayu Bali Nol. This reverses that one row of
-- the 2026-10-17 retirement (migration 327). The other four retired rows stay
-- retired.
--
-- The row was soft-retired, so it still exists and live practices still
-- reference it. This migration only flips it back and asserts the price at
-- 1.600.000 IDR, the figure on the official sheet in the same PR. The issuer
-- does not change: entities.py already routes this code to issuer 1.
--
-- Numbered 379: main is at 377 and open PR #722 carries 378.
--
-- Idempotent: re-asserts the same two values on rerun.

UPDATE practice_types
SET is_active = true,
    base_price = 1600000,
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'other_driving_license';

-- Postcondition: refuse to record this migration as applied unless the row
-- exists, is on sale and carries the ruled price.
DO $$
DECLARE
    row_active boolean;
    row_price numeric;
BEGIN
    SELECT is_active, base_price
      INTO row_active, row_price
      FROM practice_types
     WHERE code = 'other_driving_license';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration 379: practice_types.other_driving_license does not exist';
    END IF;

    IF row_active IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'migration 379: practice_types.other_driving_license is not active';
    END IF;

    IF row_price IS DISTINCT FROM 1600000 THEN
        RAISE EXCEPTION
            'migration 379: practice_types.other_driving_license is %, expected 1600000',
            row_price;
    END IF;
END;
$$;

-- === ROLLBACK ===

-- Soft retirement again, as in migration 327. base_price stays so historical
-- quotes remain readable.
UPDATE practice_types
SET is_active = false,
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'other_driving_license';
