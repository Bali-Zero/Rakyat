-- Migration 355: persist invoice issuer binding.
-- Existing NULL bindings are legacy invoices issued by the default entity.
-- No historical routing or automatic backfill is performed.
-- depends: 353

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'invoices'
    ) THEN
        RAISE NOTICE
            'Migration 355: invoices table not present; skipping (CI/fresh schema).';
        RETURN;
    END IF;

    ALTER TABLE public.invoices
        ADD COLUMN IF NOT EXISTS issuer_entity_key TEXT NULL;

    ALTER TABLE public.invoices
        ADD COLUMN IF NOT EXISTS issuer_routed_at TIMESTAMPTZ NULL;

    COMMENT ON COLUMN public.invoices.issuer_entity_key IS
        'Immutable invoice issuer registry key. NULL identifies a legacy unbound invoice, which must retain the default issuer when adopted. Never infer a legacy issuer from practice type or created_at.';

    COMMENT ON COLUMN public.invoices.issuer_routed_at IS
        'Database timestamp sampled when the issuer binding was selected. Preserved on regeneration. For legacy adoption this is the adoption instant, not the original invoice issue time; NULL accompanies an unbound legacy invoice.';
END
$migration$;

-- === ROLLBACK ===

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'invoices'
    ) THEN
        ALTER TABLE public.invoices
            DROP COLUMN IF EXISTS issuer_routed_at;

        ALTER TABLE public.invoices
            DROP COLUMN IF EXISTS issuer_entity_key;
    END IF;
END
$migration$;
