-- Migration 356: practices.invoice_company_id, the company an invoice is addressed to.
-- NULL means "bill the individual" (today's behaviour); no backfill.
-- No index on purpose: nothing filters by this column, and building one would hold the
-- table lock inside the migration transaction.
-- depends: 355

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'practices'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'companies'
    ) THEN
        RAISE NOTICE 'Migration 356: practices/companies not present; skipping (CI/fresh schema).';
        RETURN;
    END IF;

    ALTER TABLE public.practices
        ADD COLUMN IF NOT EXISTS invoice_company_id INTEGER NULL
        REFERENCES public.companies(id) ON DELETE SET NULL;

    COMMENT ON COLUMN public.practices.invoice_company_id IS
        'Company the invoice is addressed to (BILL TO). NULL = bill the individual client. Set from the practice form; typing a new name creates the company and the client link.';
END
$migration$;

-- === ROLLBACK ===

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'practices'
    ) THEN
        ALTER TABLE public.practices DROP COLUMN IF EXISTS invoice_company_id;
    END IF;
END
$migration$;
