-- ============================================================
-- 354_hr_employees_profile.sql
-- The staff HR card lives in hr_employees: one jsonb column for the
-- identity / employer / contract / document facts that have no column of
-- their own. The scalar HR fields already stored here (npwp, ptkp_status,
-- bpjs numbers, bank details, hire_date, is_active) stay where they are.
--
-- Date: 2026-11-11
-- hr_employees is created by the legacy python migration_068, not by a v2 file:
-- a database without it must FAIL here (never record 354 as applied with the
-- column missing). CI builds the table in scripts/ci_bootstrap_schema.py.
--
-- Must be kept in sync with:
--   - backend/app/services/hr/STAFF_CARD.md (the contract, C1-C9)
--   - backend/app/services/hr/hr_service.py (get_card_for_member, merge_card_on)
--   - backend/app/services/hr/hr_card_models.py (StaffProfilePatch allow-list)
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Constant default: metadata-only on PG11+, no table rewrite.
ALTER TABLE hr_employees
    ADD COLUMN IF NOT EXISTS profile jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN hr_employees.profile IS
    'Staff HR card extras (jsonb object). Flat top-level keys. Identity: legal_name, nik, birth_place, birth_date, gender, address, religion, marital_status. Others: employer_entity, unit, contract {number, file_id}, docs {slot: file_id}, end_date. Written only through HRService.merge_card_on (jsonb || merge, never overwritten wholesale).';

-- === ROLLBACK ===
-- Drops the card data held in the column; applying this is a deliberate decision.
ALTER TABLE IF EXISTS hr_employees DROP COLUMN IF EXISTS profile;
