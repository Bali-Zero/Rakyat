-- ============================================================
-- 348_tax_consultant_allow_dea_candra.sql
-- Allow Dea and Candra as a client's tax consultant
-- Date: 2026-11-03
--
-- Owner decision 2026-11-03: dea@balizero.com and candra@balizero.com
-- become assignable as clients.tax_consultant, keeping their emails as
-- they are (no .tax@ sub-alias). The CHECK created by migration 093 is
-- widened: the five existing addresses stay valid (rows already hold
-- veronika.tax@ and faisha.tax@; faisha stays in the CHECK even though
-- the backend validator no longer offers it for new assignments).
-- No data change. The LKPM assignee allowlist
-- (lkpm_reports_assigned_to_check) is NOT touched.
--
-- Same DROP CONSTRAINT IF EXISTS / ADD CONSTRAINT pair as 110, with the
-- NOT VALID + VALIDATE split of 318 (clients is ~12.5k rows; the new
-- list is a strict superset, so VALIDATE cannot fail on existing rows).
--
-- Must be kept in sync with:
--   - backend/app/routers/crm_clients.py:TAX_CONSULTANT_VALUES
-- ============================================================

-- clients.tax_consultant was created by the legacy Python migration 093
-- (backend/migrations/migration_093_lkpm_assigns_and_oss_creds.py), which a
-- database built only from migrations_v2 (the CI test database) never ran.
-- Same declaration as 093, so on prod, where the column already exists as
-- VARCHAR(64) NULL, this is a no-op.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS tax_consultant VARCHAR(64);

-- Re-added in the same transaction just below (forward: a strict superset;
-- rollback: the previous list), so no guarantee is lost.
-- squawk-ignore ban-drop-constraint
ALTER TABLE clients DROP CONSTRAINT IF EXISTS clients_tax_consultant_check;

ALTER TABLE clients
  ADD CONSTRAINT clients_tax_consultant_check
  CHECK (
    tax_consultant IS NULL
    OR tax_consultant IN (
      'veronika.tax@balizero.com',
      'kadek.tax@balizero.com',
      'dewaayu.tax@balizero.com',
      'angel.tax@balizero.com',
      'faisha.tax@balizero.com',
      'dea@balizero.com',
      'candra@balizero.com'
    )
  ) NOT VALID;

ALTER TABLE clients
  VALIDATE CONSTRAINT clients_tax_consultant_check;

-- === ROLLBACK ===
-- NOT VALID on purpose: rows already assigned dea@/candra@ must not make
-- the narrower CHECK fail on history; it binds only later writes.

-- Re-added in the same transaction just below (forward: a strict superset;
-- rollback: the previous list), so no guarantee is lost.
-- squawk-ignore ban-drop-constraint
ALTER TABLE clients DROP CONSTRAINT IF EXISTS clients_tax_consultant_check;

ALTER TABLE clients
  ADD CONSTRAINT clients_tax_consultant_check
  CHECK (
    tax_consultant IS NULL
    OR tax_consultant IN (
      'veronika.tax@balizero.com',
      'kadek.tax@balizero.com',
      'dewaayu.tax@balizero.com',
      'angel.tax@balizero.com',
      'faisha.tax@balizero.com'
    )
  ) NOT VALID;
