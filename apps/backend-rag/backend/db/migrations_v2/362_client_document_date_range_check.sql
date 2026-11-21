-- ============================================================
-- 362_client_document_date_range_check.sql
-- Closes the non-finite / out-of-range date gap left after Rakyat #165 and
-- #212. #212 enforces 1900-01-01..2100-12-31 at every write point
-- (CLIENT_DOCUMENT_DATE_MIN / _MAX in services/crm/documents.py); this
-- migration makes the database enforce the same range.
--
-- 1. Repair by rule (no ids, no values in this file):
--    clients.passport_expiry outside the range, infinities included, becomes
--    the expiry_date of that client's most recent non-deleted passport
--    document whose expiry is in range, or NULL when there is none.
--    clients.visa_expiry_date, clients.kitas_expiry_date and
--    documents.expiry_date outside the range become NULL (no source).
-- 2. A CHECK on each of the four columns, added NOT VALID then VALIDATED.
--    The repair runs first: a NOT VALID CHECK still fires on every UPDATE of
--    a violating row.
--
-- clients.date_of_birth and documents.issue_date are out of scope.
-- Consumer: the CHECKs are enforced by Postgres on every write of the four
-- columns, including crm_clients_documents.py, crm_enhanced*.py,
-- client_repository.py and services/intake/validate_rules.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '120s';

DO $migration$
DECLARE
    has_passport boolean;
    has_visa boolean;
    has_kitas boolean;
    has_doc_expiry boolean;
BEGIN
    IF to_regclass('public.clients') IS NULL OR to_regclass('public.documents') IS NULL THEN
        RAISE NOTICE 'Migration 362: clients/documents not present; skipping (CI/fresh schema).';
        RETURN;
    END IF;

    -- Column guards: the CI bootstrap schema lacks clients.visa_expiry_date and
    -- clients.kitas_expiry_date (production has them), so each column is
    -- handled only where it exists.
    has_passport := EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients' AND column_name = 'passport_expiry');
    has_visa := EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients' AND column_name = 'visa_expiry_date');
    has_kitas := EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients' AND column_name = 'kitas_expiry_date');
    has_doc_expiry := EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'documents' AND column_name = 'expiry_date');

    -- 1. Repair by rule.
    IF has_passport THEN
        IF has_doc_expiry THEN
            UPDATE public.clients c
               SET passport_expiry = (
                       SELECT d.expiry_date
                         FROM public.documents d
                        WHERE d.client_id = c.id
                          AND d.document_type = 'passport'
                          AND d.deleted_at IS NULL
                          AND d.expiry_date BETWEEN DATE '1900-01-01' AND DATE '2100-12-31'
                        ORDER BY d.created_at DESC NULLS LAST, d.id DESC
                        LIMIT 1
                   )
             WHERE c.passport_expiry IS NOT NULL
               AND c.passport_expiry NOT BETWEEN DATE '1900-01-01' AND DATE '2100-12-31';
        ELSE
            UPDATE public.clients SET passport_expiry = NULL
             WHERE passport_expiry IS NOT NULL
               AND passport_expiry NOT BETWEEN DATE '1900-01-01' AND DATE '2100-12-31';
        END IF;
    END IF;

    IF has_visa THEN
        UPDATE public.clients
           SET visa_expiry_date = NULL
         WHERE visa_expiry_date IS NOT NULL
           AND visa_expiry_date NOT BETWEEN DATE '1900-01-01' AND DATE '2100-12-31';
    END IF;

    IF has_kitas THEN
        UPDATE public.clients
           SET kitas_expiry_date = NULL
         WHERE kitas_expiry_date IS NOT NULL
           AND kitas_expiry_date NOT BETWEEN DATE '1900-01-01' AND DATE '2100-12-31';
    END IF;

    IF has_doc_expiry THEN
        UPDATE public.documents
           SET expiry_date = NULL
         WHERE expiry_date IS NOT NULL
           AND expiry_date NOT BETWEEN DATE '1900-01-01' AND DATE '2100-12-31';
    END IF;

    -- 2. Constraints, NOT VALID then VALIDATE.
    IF has_passport AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clients_passport_expiry_range_chk'
                      AND conrelid = 'public.clients'::regclass) THEN
        ALTER TABLE public.clients ADD CONSTRAINT clients_passport_expiry_range_chk
            CHECK (passport_expiry IS NULL
                   OR passport_expiry BETWEEN DATE '1900-01-01' AND DATE '2100-12-31') NOT VALID;
    END IF;
    IF has_visa AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clients_visa_expiry_date_range_chk'
                      AND conrelid = 'public.clients'::regclass) THEN
        ALTER TABLE public.clients ADD CONSTRAINT clients_visa_expiry_date_range_chk
            CHECK (visa_expiry_date IS NULL
                   OR visa_expiry_date BETWEEN DATE '1900-01-01' AND DATE '2100-12-31') NOT VALID;
    END IF;
    IF has_kitas AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clients_kitas_expiry_date_range_chk'
                      AND conrelid = 'public.clients'::regclass) THEN
        ALTER TABLE public.clients ADD CONSTRAINT clients_kitas_expiry_date_range_chk
            CHECK (kitas_expiry_date IS NULL
                   OR kitas_expiry_date BETWEEN DATE '1900-01-01' AND DATE '2100-12-31') NOT VALID;
    END IF;
    IF has_doc_expiry AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_expiry_date_range_chk'
                      AND conrelid = 'public.documents'::regclass) THEN
        ALTER TABLE public.documents ADD CONSTRAINT documents_expiry_date_range_chk
            CHECK (expiry_date IS NULL
                   OR expiry_date BETWEEN DATE '1900-01-01' AND DATE '2100-12-31') NOT VALID;
    END IF;

    IF has_passport THEN
        ALTER TABLE public.clients VALIDATE CONSTRAINT clients_passport_expiry_range_chk;
    END IF;
    IF has_visa THEN
        ALTER TABLE public.clients VALIDATE CONSTRAINT clients_visa_expiry_date_range_chk;
    END IF;
    IF has_kitas THEN
        ALTER TABLE public.clients VALIDATE CONSTRAINT clients_kitas_expiry_date_range_chk;
    END IF;
    IF has_doc_expiry THEN
        ALTER TABLE public.documents VALIDATE CONSTRAINT documents_expiry_date_range_chk;
    END IF;
END
$migration$;

-- === ROLLBACK ===
-- The repair is not reversible; only the constraints are dropped.

ALTER TABLE IF EXISTS public.clients DROP CONSTRAINT IF EXISTS clients_passport_expiry_range_chk;
ALTER TABLE IF EXISTS public.clients DROP CONSTRAINT IF EXISTS clients_visa_expiry_date_range_chk;
ALTER TABLE IF EXISTS public.clients DROP CONSTRAINT IF EXISTS clients_kitas_expiry_date_range_chk;
ALTER TABLE IF EXISTS public.documents DROP CONSTRAINT IF EXISTS documents_expiry_date_range_chk;
