-- Migration 380: add the D12 Business Investigation extension to the catalogue
--
-- Ruled by the owner on 2026-11-23 (decision K134): D12 can be extended once,
-- for a further 180 days, and Bali Zero sells that extension at 3.750.000 IDR,
-- invoiced by CV Bayu Santero. The row sits in Visa Extension next to
-- ext_b1_voa and ext_c1_tourism, so it appears in the New Process dropdown.
--
-- Numbered 380: main is at 377, open PR #722 carries 378 and the Driving
-- License PR carries 379.
--
-- Idempotent: ON CONFLICT(code) DO UPDATE, the pattern of migration 221.

INSERT INTO practice_types (
    code, name, description, category, base_price,
    typical_duration_days, is_active
)
VALUES (
    'ext_d12',
    'D12 Business Investigation — Extension',
    'One extension of the D12 Business Investigation stay permit for a further 180 days.',
    'visa_extension',
    3750000,
    NULL,
    true
)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    base_price = EXCLUDED.base_price,
    typical_duration_days = EXCLUDED.typical_duration_days,
    is_active = true,
    updated_at = CURRENT_TIMESTAMP;

-- Postcondition: refuse to record this migration as applied unless the row
-- exists, is on sale, sits in Visa Extension and carries the ruled price.
DO $$
DECLARE
    row_active boolean;
    row_price numeric;
    row_category text;
BEGIN
    SELECT is_active, base_price, category
      INTO row_active, row_price, row_category
      FROM practice_types
     WHERE code = 'ext_d12';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration 380: practice_types.ext_d12 does not exist';
    END IF;

    IF row_active IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'migration 380: practice_types.ext_d12 is not active';
    END IF;

    IF row_price IS DISTINCT FROM 3750000 THEN
        RAISE EXCEPTION 'migration 380: practice_types.ext_d12 is %, expected 3750000', row_price;
    END IF;

    IF row_category IS DISTINCT FROM 'visa_extension' THEN
        RAISE EXCEPTION
            'migration 380: practice_types.ext_d12 category is %, expected visa_extension',
            row_category;
    END IF;
END;
$$;

-- === ROLLBACK ===

-- Soft-disable, never a hard removal: practices reference practice_types by
-- foreign key, so a row already in use cannot be deleted.
UPDATE practice_types
SET is_active = false,
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'ext_d12';
