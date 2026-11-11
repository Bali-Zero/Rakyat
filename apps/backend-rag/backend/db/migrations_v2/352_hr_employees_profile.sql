-- ============================================================
-- 352_hr_employees_profile.sql
-- The staff HR card lives in hr_employees: one jsonb column for the
-- identity / employer / contract / document facts that have no column of
-- their own. The scalar HR fields already stored here (npwp, ptkp_status,
-- bpjs numbers, bank details, hire_date, is_active) stay where they are.
--
-- Date: 2026-11-11
-- hr_employees is created by the legacy python migration_068 (not by a v2
-- file), so the statements below are guarded and no-op on a database that
-- does not have the table (CI scratch databases); production has it.
--
-- Must be kept in sync with:
--   - backend/app/services/hr/hr_service.py (get_card_*, merge_card)
--   - backend/app/routers/hr.py (StaffProfilePatch allow-list)
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

DO $mig$
BEGIN
    IF to_regclass('public.hr_employees') IS NULL THEN
        RAISE NOTICE 'hr_employees absent: 352 is a no-op on this database';
        RETURN;
    END IF;

    -- Constant default: metadata-only on PG11+, no table rewrite.
    ALTER TABLE hr_employees
        ADD COLUMN IF NOT EXISTS profile jsonb NOT NULL DEFAULT '{}'::jsonb;

    COMMENT ON COLUMN hr_employees.profile IS
        'Staff HR card extras (jsonb object). Flat top-level keys. Identity: legal_name, nik, birth_place, birth_date, gender, address, religion, marital_status. Others: employer_entity, unit, contract {number, file_id}, docs {slot: file_id}, end_date. Written only through HRService.merge_card (jsonb || merge, never overwritten wholesale).';
END
$mig$;
