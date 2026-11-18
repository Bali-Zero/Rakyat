-- ============================================================
-- 364_lkpm_allowlist_dea_candra.sql
-- Add Dea and Candra to the lkpm_reports assignee allowlist
-- Date: 2026-11-18
--
-- Owner decision 2026-11-18: dea@balizero.com and candra@balizero.com join
-- the LKPM team, keeping their main inboxes (no .tax@ sub-alias, like
-- krisna@ in 110). The CHECK lkpm_reports_assigned_to_check is widened to a
-- strict superset: every address valid before stays valid, so no existing
-- row can fail. No data change. The clients tax-consultant CHECK was already
-- widened by 348 and is NOT touched.
--
-- Same DROP CONSTRAINT IF EXISTS / ADD CONSTRAINT pair as 110, with the
-- NOT VALID + VALIDATE split of 348 / 318.
--
-- Must be kept in sync with:
--   - backend/app/routers/lkpm.py:LKPM_ASSIGNEES
--   - apps/mouth/src/lib/workspace/roster-directory.ts:LKPM_ASSIGNEES
-- ============================================================

-- Re-added in the same transaction just below (forward: a strict superset;
-- rollback: the previous list), so no guarantee is lost.
-- squawk-ignore ban-drop-constraint
ALTER TABLE lkpm_reports DROP CONSTRAINT IF EXISTS lkpm_reports_assigned_to_check;

ALTER TABLE lkpm_reports
  ADD CONSTRAINT lkpm_reports_assigned_to_check
  CHECK (
    lkpm_assigned_to IS NULL
    OR lkpm_assigned_to IN (
      'veronika.tax@balizero.com',
      'kadek.tax@balizero.com',
      'dewaayu.tax@balizero.com',
      'angel.tax@balizero.com',
      'faisha.tax@balizero.com',
      'krisna@balizero.com',
      'dea@balizero.com',
      'candra@balizero.com'
    )
  ) NOT VALID;

ALTER TABLE lkpm_reports
  VALIDATE CONSTRAINT lkpm_reports_assigned_to_check;

-- === ROLLBACK ===
-- NOT VALID on purpose: rows already assigned dea@/candra@ must not make
-- the narrower CHECK fail on history; it binds only later writes.

-- Re-added in the same transaction just below (forward: a strict superset;
-- rollback: the previous list), so no guarantee is lost.
-- squawk-ignore ban-drop-constraint
ALTER TABLE lkpm_reports DROP CONSTRAINT IF EXISTS lkpm_reports_assigned_to_check;

ALTER TABLE lkpm_reports
  ADD CONSTRAINT lkpm_reports_assigned_to_check
  CHECK (
    lkpm_assigned_to IS NULL
    OR lkpm_assigned_to IN (
      'veronika.tax@balizero.com',
      'kadek.tax@balizero.com',
      'dewaayu.tax@balizero.com',
      'angel.tax@balizero.com',
      'faisha.tax@balizero.com',
      'krisna@balizero.com'
    )
  ) NOT VALID;
