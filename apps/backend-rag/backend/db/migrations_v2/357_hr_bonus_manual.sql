-- ============================================================
-- 357_hr_bonus_manual.sql
-- A bonus can be entered by hand: hr_bonus_ledger gains a source
-- ('practice' | 'manual'), a reason and the HR admin who added it, and
-- practice_id becomes nullable for manual rows.
-- Date: 2026-11-12
--
-- Owner decision 2026-11-12: the tax team completes almost no practices
-- in kita, so bonuses for tax services cannot come only from the
-- practice-completion hooks. HR admins add a bonus for any person with an
-- amount and a reason. Every existing row keeps source = 'practice'.
--
-- Shape contract (ck_hr_bonus_source_shape):
--   practice -> practice_id NOT NULL
--   manual   -> practice_id NULL, reason present
-- UNIQUE(practice_id, employee_id) is untouched: NULL practice_id rows
-- never collide in a unique index, so several manual bonuses for one
-- person are allowed.
--
-- The trigger function hr_validate_completed_practice (migration_068)
-- is replaced so it validates only when NEW.practice_id IS NOT NULL; the
-- trigger itself (trg_hr_bonus_validate) is unchanged.
--
-- hr_bonus_ledger is created by the legacy python migration_068, not by a
-- v2 file: a database without it must FAIL here. CI builds it in
-- scripts/ci_bootstrap_schema.py.
--
-- Same NOT VALID + VALIDATE split as 348/318: existing rows satisfy all
-- three constraints by construction (source defaults to 'practice' and
-- practice_id was NOT NULL), so VALIDATE cannot fail.
--
-- Must be kept in sync with:
--   - backend/app/services/hr/hr_service.py (create_manual_bonus)
--   - backend/app/routers/hr.py (ManualBonusCreate: reason 3..300)
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Manual bonuses have no practice; the shape CHECK below keeps practice rows non-null.
-- squawk-ignore ban-drop-not-null
ALTER TABLE hr_bonus_ledger ALTER COLUMN practice_id DROP NOT NULL;

-- Constant default: metadata-only on PG11+, no table rewrite.
ALTER TABLE hr_bonus_ledger ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'practice';
ALTER TABLE hr_bonus_ledger ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE hr_bonus_ledger ADD COLUMN IF NOT EXISTS created_by TEXT;

COMMENT ON COLUMN hr_bonus_ledger.source IS
    'practice = created by a practice-completion hook (practice_id set); manual = entered by an HR admin (practice_id NULL, reason set).';
COMMENT ON COLUMN hr_bonus_ledger.created_by IS
    'Email of the HR admin who entered a manual bonus; NULL for practice bonuses. Admin view only.';

-- Re-added just below with the new shape, so no guarantee is lost.
-- squawk-ignore ban-drop-constraint
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_source;
ALTER TABLE hr_bonus_ledger
    ADD CONSTRAINT ck_hr_bonus_source CHECK (source IN ('practice', 'manual')) NOT VALID;
ALTER TABLE hr_bonus_ledger VALIDATE CONSTRAINT ck_hr_bonus_source;

-- squawk-ignore ban-drop-constraint
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_reason_len;
ALTER TABLE hr_bonus_ledger
    ADD CONSTRAINT ck_hr_bonus_reason_len CHECK (reason IS NULL OR char_length(reason) <= 300) NOT VALID;
ALTER TABLE hr_bonus_ledger VALIDATE CONSTRAINT ck_hr_bonus_reason_len;

-- squawk-ignore ban-drop-constraint
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_source_shape;
ALTER TABLE hr_bonus_ledger
    ADD CONSTRAINT ck_hr_bonus_source_shape CHECK (
        (source = 'practice' AND practice_id IS NOT NULL)
        OR (source = 'manual' AND practice_id IS NULL
            AND reason IS NOT NULL AND length(btrim(reason)) > 0)
    ) NOT VALID;
ALTER TABLE hr_bonus_ledger VALIDATE CONSTRAINT ck_hr_bonus_source_shape;

CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
DECLARE
    v_status TEXT;
BEGIN
    IF NEW.practice_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT status INTO v_status FROM practices WHERE id = NEW.practice_id;
    IF v_status IS NULL OR v_status <> 'completed' THEN
        RAISE EXCEPTION 'Practice % must be completed before bonus entry (status=%)',
            NEW.practice_id, COALESCE(v_status, 'NOT_FOUND');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- === ROLLBACK ===
-- SET NOT NULL comes first on purpose: with manual rows present it fails and
-- aborts the whole rollback, so manual bonuses are never silently lost. Delete
-- or convert them by hand before rolling back.
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_source_shape;
ALTER TABLE hr_bonus_ledger ALTER COLUMN practice_id SET NOT NULL;
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_reason_len;
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT IF EXISTS ck_hr_bonus_source;
ALTER TABLE hr_bonus_ledger DROP COLUMN IF EXISTS created_by;
ALTER TABLE hr_bonus_ledger DROP COLUMN IF EXISTS reason;
ALTER TABLE hr_bonus_ledger DROP COLUMN IF EXISTS source;

CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM practices WHERE id = NEW.practice_id;
    IF v_status IS NULL OR v_status <> 'completed' THEN
        RAISE EXCEPTION 'Practice % must be completed before bonus entry (status=%)',
            NEW.practice_id, COALESCE(v_status, 'NOT_FOUND');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
