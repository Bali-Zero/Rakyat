-- Bonus reform C, PR2a (owner rule 2026-11-27): from November 2026 a bonus belongs to its Bali calendar
-- month, and that month is closed by its own bonus run, not by the salary period. This file moves the
-- LOCK only; closing and paying a bonus month (rows in hr_bonus_runs) and the salary payroll's bonus
-- inputs are PR3. Release window: the old image keeps serving after this commits; it reads none of the
-- new objects, and every object here is new, so nothing waits on application traffic.
-- Ownership: the runner assumes backend_rag_v2 (migration_base.py:39-56), so it owns every object, as in
-- 384/386/388. Rollback: roll the IMAGE back first (the new code calls hr_bonus_month_closed on every
-- bonus write), then this file. Roll the image back only while no new-cycle bonus activity happened:
-- once a bonus awarded in November 2026 or later was booked or touched after this file applied, the old
-- image could not approve or pay it (its salary month is paid), so the rollback below refuses and the
-- way out is to roll forward.

CREATE FUNCTION hr_bonus_run_first_month() RETURNS date
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT DATE '2026-11-01' $$;

-- A row exists only once a bonus month is closed: approved, then paid.
CREATE TABLE hr_bonus_runs (
    bonus_month DATE PRIMARY KEY
        CHECK (bonus_month >= hr_bonus_run_first_month() AND EXTRACT(DAY FROM bonus_month) = 1),
    status TEXT NOT NULL CHECK (status IN ('approved', 'paid')),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at TIMESTAMPTZ,
    CHECK ((status = 'paid') = (paid_at IS NOT NULL))
);

-- Like hr_enforce_period_lock (migration_068_hr_payroll.py:277-311): a closed month stays closed.
CREATE FUNCTION hr_bonus_run_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Bonus run % is closed and is never deleted', OLD.bonus_month;
    END IF;
    IF OLD.status = 'paid' OR NEW.status <> 'paid' OR NEW.bonus_month <> OLD.bonus_month THEN
        RAISE EXCEPTION 'Bonus run % is closed: an approved run only becomes paid', OLD.bonus_month;
    END IF;
    NEW.paid_at := COALESCE(NEW.paid_at, now());
    RETURN NEW;
END $$;
CREATE TRIGGER trg_hr_bonus_run_lock BEFORE UPDATE OR DELETE ON hr_bonus_runs
    FOR EACH ROW EXECUTE FUNCTION hr_bonus_run_lock();

-- Before the cycle: the salary rule, unchanged. From it: the bonus run. The salary period never closes
-- a bonus month again.
CREATE FUNCTION hr_bonus_month_closed(p_month date) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN p_month < hr_bonus_run_first_month() THEN EXISTS (
                    SELECT 1 FROM hr_payroll_periods
                    WHERE payroll_month = EXTRACT(MONTH FROM p_month)
                      AND payroll_year = EXTRACT(YEAR FROM p_month)
                      AND status IN ('approved', 'paid'))
                ELSE EXISTS (SELECT 1 FROM hr_bonus_runs
                             WHERE bonus_month = date_trunc('month', p_month)::date) END
$$;

-- Read-only consumers (the owning session's prod checks) see the new table; guarded as in 387:111-118.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nuzantara_readonly') THEN
        GRANT SELECT ON TABLE hr_bonus_runs TO nuzantara_readonly;
    END IF;
END $$;

-- === ROLLBACK ===
-- Image first (see the header). A closed bonus month is history: roll forward instead.
DO $$
DECLARE
    applied timestamptz := '-infinity'::timestamptz;  -- applied outside the runner: most conservative
BEGIN
    IF EXISTS (SELECT 1 FROM hr_bonus_runs) THEN
        RAISE EXCEPTION 'Preserve closed bonus months before rolling back migration 393';
    END IF;
    IF to_regclass('_schema_versions') IS NOT NULL THEN
        applied := COALESCE((SELECT max(executed_at) FROM _schema_versions WHERE migration_number = 393),
                            '-infinity'::timestamptz);
    END IF;
    IF to_regclass('hr_bonus_ledger') IS NOT NULL AND EXISTS (
        SELECT 1 FROM hr_bonus_ledger
        WHERE (awarded_at AT TIME ZONE 'Asia/Makassar') >= hr_bonus_run_first_month()
          AND GREATEST(created_at, updated_at) >= applied) THEN
        RAISE EXCEPTION 'Bonus activity for the new cycle happened after migration 393: roll forward instead';
    END IF;
END $$;
DROP FUNCTION hr_bonus_month_closed(date);
DROP TABLE hr_bonus_runs;
DROP FUNCTION hr_bonus_run_lock();
DROP FUNCTION hr_bonus_run_first_month();
