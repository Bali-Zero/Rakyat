-- Historical tax completions are imported evidence, not sellable services.
-- The inactive type cannot be selected by the ordinary create-practice route.
INSERT INTO practice_types (code, name, description, category, base_price, typical_duration_days, is_active)
VALUES (
    'tax_lkpm_quarterly_historical',
    'LKPM Quarterly Report (Historical)',
    'Evidence-backed historical import only; never quote, invoice, or sell.',
    'tax', NULL, NULL, false
)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    base_price = NULL,
    typical_duration_days = NULL,
    is_active = false;

ALTER TABLE public.practices
    ADD COLUMN IF NOT EXISTS historical_completion_identity TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_practices_historical_completion_identity
    ON public.practices (historical_completion_identity)
    WHERE historical_completion_identity IS NOT NULL;

-- === ROLLBACK ===
DROP INDEX IF EXISTS public.uq_practices_historical_completion_identity;
ALTER TABLE public.practices DROP COLUMN IF EXISTS historical_completion_identity;
DELETE FROM public.practice_types WHERE code = 'tax_lkpm_quarterly_historical';
