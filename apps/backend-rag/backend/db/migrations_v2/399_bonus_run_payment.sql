-- depends: 393
-- Bonus reform C, PR3a (owner 2026-11-27/28): from Nov 2026 a bonus month is closed and paid by its own run; the
-- salary carries, defers and holds no bonus (R4). No tax (O1). Release window: the old image calls the R4 bodies
-- (its Dec salary carries no bonus), reads the view by column name and hr_bonus_runs via hr_bonus_month_closed,
-- writes the ledger, never bonus_run_id. NEVER WAITS ON A RELATION: the prologue takes every lock up front,
-- NOWAIT; a busy writer is a 55P03 the runner retries; no EXCEPTION handler (394). Rollback: preflight, image,
-- then this file. Prologue (the rollback repeats it): HR writers take the allocation lock first, so no salary
-- transaction runs half under 386 and half under R4; delivery writers take none, NOWAIT covers them.
DO $$ BEGIN IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
  RAISE EXCEPTION '399: bonus payout allocation is busy' USING ERRCODE = '55P03'; END IF; END $$;
LOCK TABLE hr_delivery_bonus_events, hr_bonus_ledger, hr_bonus_runs, hr_payroll_periods, practice_visa_followups,
  hr_visa_bonus_hold_periods, hr_bonus_deferral_causes, hr_visa_bonus_obligations IN ACCESS EXCLUSIVE MODE NOWAIT;

-- team_members.id of the approver and the payer; no FK (SET NULL would hit the lock trigger)
ALTER TABLE hr_bonus_runs ADD COLUMN approved_by TEXT, ADD COLUMN paid_by TEXT;
CREATE OR REPLACE FUNCTION hr_bonus_run_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Bonus run % is closed and is never deleted', OLD.bonus_month;
    END IF;
    IF OLD.status = 'paid' OR NEW.status <> 'paid' OR NEW.bonus_month <> OLD.bonus_month
       OR NEW.approved_at IS DISTINCT FROM OLD.approved_at OR NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
        RAISE EXCEPTION 'Bonus run % is closed: an approved run only becomes paid', OLD.bonus_month;
    END IF;
    NEW.paid_at := COALESCE(NEW.paid_at, now());
    RETURN NEW;
END $$;

-- One payout per row: a salary period or a run, never both; a paid row from the cycle on has one of them.
ALTER TABLE hr_bonus_ledger
  ADD COLUMN bonus_run_id DATE REFERENCES hr_bonus_runs(bonus_month) ON DELETE RESTRICT,
  ADD CONSTRAINT ck_hr_bonus_one_payout CHECK (bonus_run_id IS NULL OR payroll_period_id IS NULL),
  ADD CONSTRAINT ck_hr_bonus_paid_has_payout CHECK (status <> 'paid' OR payroll_period_id IS NOT NULL
      OR bonus_run_id IS NOT NULL OR awarded_at < TIMESTAMPTZ '2026-11-01 00:00:00+08');
CREATE INDEX ix_hr_bonus_run ON hr_bonus_ledger (bonus_run_id) WHERE bonus_run_id IS NOT NULL;
-- A hold is bound to exactly one of a salary period or a run.
ALTER TABLE hr_visa_bonus_hold_periods
  ADD COLUMN bonus_run_id DATE REFERENCES hr_bonus_runs(bonus_month) ON DELETE RESTRICT,
  ALTER COLUMN payroll_period_id DROP NOT NULL,
  ADD CONSTRAINT ck_hr_visa_hold_one_payout CHECK (num_nonnulls(payroll_period_id, bonus_run_id) = 1);

CREATE OR REPLACE VIEW hr_visa_bonus_obligations AS  -- 386, bound_month widened, one column appended
SELECT f.practice_id, e.employee_id, f.service_code, f.slot_key, f.started_on, f.due_on, f.email_state,
       h.payroll_period_id AS bound_period_id, COALESCE(p.period_start, h.bonus_run_id) AS bound_month,
       GREATEST(date_trunc('month', f.due_on::timestamp)::date,
                (date_trunc('month', f.started_on::timestamp) + interval '1 month')::date) AS payout_from_month,
       h.bonus_run_id AS bound_run_month
FROM practice_visa_followups f JOIN hr_delivery_bonus_events e ON e.practice_id=f.practice_id
LEFT JOIN hr_visa_bonus_hold_periods h ON h.practice_id=f.practice_id
LEFT JOIN hr_payroll_periods p ON p.id=h.payroll_period_id
WHERE f.fulfilled_at IS NULL AND e.employee_id IS NOT NULL;

-- R4: from the cycle on the salary reserves, holds and pays no bonus; a run-bound hold is not a salary hold.
CREATE OR REPLACE FUNCTION hr_visa_holds_for_period(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (practice_id INTEGER,employee_id INTEGER)
LANGUAGE SQL STABLE AS $$
    SELECT o.practice_id,o.employee_id FROM hr_visa_bonus_obligations o
    WHERE target_month < hr_bonus_run_first_month() AND (o.bound_period_id=target_period
       OR (o.bound_period_id IS NULL AND o.bound_run_month IS NULL AND o.due_on <= as_of
           AND o.payout_from_month <= target_month
           AND NOT EXISTS (
               SELECT 1 FROM hr_payroll_periods earlier WHERE earlier.status IN ('draft','calculated')
                 AND earlier.period_start >= o.payout_from_month AND earlier.period_start < target_month
           )))
$$;
CREATE OR REPLACE FUNCTION hr_payroll_bonus_inputs(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (bonus_id BIGINT, employee_id INTEGER, amount_idr BIGINT, status TEXT, held BOOLEAN)
LANGUAGE SQL STABLE AS $$
    SELECT b.id, b.employee_id, b.amount_idr, b.status::text,
           EXISTS (SELECT 1 FROM hr_visa_holds_for_period(target_month,target_period,as_of) h
                   WHERE h.employee_id=b.employee_id)
           OR EXISTS (
               SELECT 1 FROM hr_bonus_deferral_causes c JOIN hr_visa_bonus_obligations o USING(practice_id)
                WHERE c.bonus_id=b.id
           )
    FROM hr_bonus_ledger b
    WHERE target_month < hr_bonus_run_first_month() AND b.bonus_run_id IS NULL
      AND b.status IN ('pending','approved')
      AND (b.payroll_period_id IS NULL OR b.payroll_period_id=target_period)
      AND (
          date_trunc('month', b.awarded_at AT TIME ZONE 'Asia/Makassar')::date=target_month
          OR (b.deferred_from < target_month AND NOT EXISTS (
              SELECT 1 FROM hr_payroll_periods earlier WHERE earlier.status IN ('draft','calculated')
                AND earlier.period_start > b.deferred_from AND earlier.period_start < target_month
          ))
      )
$$;

-- The run's own holds and inputs (they need the month and as_of). Runs close in order: no "earlier period" clause.
CREATE FUNCTION hr_bonus_run_holds(p_month date, p_as_of date)
RETURNS TABLE (practice_id integer, employee_id integer) LANGUAGE sql STABLE AS $$
  SELECT o.practice_id, o.employee_id FROM hr_visa_bonus_obligations o
  WHERE p_month >= hr_bonus_run_first_month()
    AND (o.bound_run_month = p_month OR (o.bound_period_id IS NULL AND o.bound_run_month IS NULL
         AND o.due_on <= p_as_of AND o.payout_from_month <= p_month)) $$;
CREATE FUNCTION hr_bonus_run_inputs(p_month date, p_as_of date)
RETURNS TABLE (bonus_id bigint, employee_id integer, amount_idr bigint, status text, held boolean)
LANGUAGE sql STABLE AS $$
  SELECT b.id, b.employee_id, b.amount_idr, b.status::text,
         EXISTS (SELECT 1 FROM hr_bonus_run_holds(p_month, p_as_of) h WHERE h.employee_id = b.employee_id)
         OR EXISTS (SELECT 1 FROM hr_bonus_deferral_causes c JOIN hr_visa_bonus_obligations o USING (practice_id)
                    WHERE c.bonus_id = b.id)
  FROM hr_bonus_ledger b
  WHERE p_month >= hr_bonus_run_first_month() AND b.status IN ('pending','approved')
    AND b.payroll_period_id IS NULL AND (b.bonus_run_id IS NULL OR b.bonus_run_id = p_month)
    AND (date_trunc('month', b.awarded_at AT TIME ZONE 'Asia/Makassar')::date = p_month
         OR b.deferred_from < p_month) $$;  -- non-deferred pre-Nov rows never enter a run

-- === ROLLBACK ===
-- After the preflight and the image (header). First the forward's prologue, verbatim: try-lock, then LOCK TABLE.
DO $$ BEGIN IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
  RAISE EXCEPTION '399: bonus payout allocation is busy' USING ERRCODE = '55P03'; END IF; END $$;
LOCK TABLE hr_delivery_bonus_events, hr_bonus_ledger, hr_bonus_runs, hr_payroll_periods, practice_visa_followups,
  hr_visa_bonus_hold_periods, hr_bonus_deferral_causes, hr_visa_bonus_obligations IN ACCESS EXCLUSIVE MODE NOWAIT;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM hr_bonus_runs) THEN
    RAISE EXCEPTION 'Preserve closed bonus runs before rolling back migration 399: roll forward'; END IF;
  -- State, never time (approved_at = the approving transaction's start); under 386 an undeferred row is paid
  -- only by its own month's salary. (1) A month from December on closed under 399 has no bonus on its slips,
  -- and after a rollback nothing pays its rows, pending ones approved later included: refuse on the period alone.
  IF EXISTS (SELECT 1 FROM hr_payroll_periods WHERE status IN ('approved','paid')
      AND period_start >= (hr_bonus_run_first_month() + interval '1 month')::date) THEN
    RAISE EXCEPTION 'A salary month from December 2026 on is closed: its bonuses need a run, roll forward'; END IF;
  -- (2) an approved row under its closed salary month (after (1), only November) is payable only by a run.
  IF EXISTS (SELECT 1 FROM hr_bonus_ledger b WHERE b.status = 'approved' AND b.payroll_period_id IS NULL
      AND b.bonus_run_id IS NULL AND b.deferred_from IS NULL
      AND date_trunc('month', b.awarded_at AT TIME ZONE 'Asia/Makassar')::date >= hr_bonus_run_first_month()
      AND EXISTS (SELECT 1 FROM hr_payroll_periods p WHERE p.status IN ('approved','paid')
        AND p.period_start = date_trunc('month', b.awarded_at AT TIME ZONE 'Asia/Makassar')::date)) THEN
    RAISE EXCEPTION 'An approved bonus from November 2026 on can be paid only by a bonus run: roll forward'; END IF;
END $$;
DROP FUNCTION hr_bonus_run_inputs(date, date);
DROP FUNCTION hr_bonus_run_holds(date, date);
CREATE OR REPLACE FUNCTION hr_visa_holds_for_period(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (practice_id INTEGER,employee_id INTEGER)
LANGUAGE SQL STABLE AS $$
    SELECT o.practice_id,o.employee_id FROM hr_visa_bonus_obligations o
    WHERE o.bound_period_id=target_period
       OR (o.bound_period_id IS NULL AND o.due_on <= as_of AND o.payout_from_month <= target_month
           AND NOT EXISTS (
               SELECT 1 FROM hr_payroll_periods earlier WHERE earlier.status IN ('draft','calculated')
                 AND earlier.period_start >= o.payout_from_month AND earlier.period_start < target_month
           ))
$$;
CREATE OR REPLACE FUNCTION hr_payroll_bonus_inputs(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (bonus_id BIGINT, employee_id INTEGER, amount_idr BIGINT, status TEXT, held BOOLEAN)
LANGUAGE SQL STABLE AS $$
    SELECT b.id, b.employee_id, b.amount_idr, b.status::text,
           EXISTS (SELECT 1 FROM hr_visa_holds_for_period(target_month,target_period,as_of) h
                   WHERE h.employee_id=b.employee_id)
           OR EXISTS (
               SELECT 1 FROM hr_bonus_deferral_causes c JOIN hr_visa_bonus_obligations o USING(practice_id)
                WHERE c.bonus_id=b.id
           )
    FROM hr_bonus_ledger b
    WHERE b.status IN ('pending','approved')
      AND (b.payroll_period_id IS NULL OR b.payroll_period_id=target_period)
      AND (
          date_trunc('month', b.awarded_at AT TIME ZONE 'Asia/Makassar')::date=target_month
          OR (b.deferred_from < target_month AND NOT EXISTS (
              SELECT 1 FROM hr_payroll_periods earlier WHERE earlier.status IN ('draft','calculated')
                AND earlier.period_start > b.deferred_from AND earlier.period_start < target_month
          ))
      )
$$;
DROP VIEW hr_visa_bonus_obligations;
CREATE VIEW hr_visa_bonus_obligations AS
SELECT f.practice_id, e.employee_id, f.service_code, f.slot_key, f.started_on,
       f.due_on, f.email_state, h.payroll_period_id AS bound_period_id, p.period_start AS bound_month,
       GREATEST(date_trunc('month', f.due_on::timestamp)::date,
                (date_trunc('month', f.started_on::timestamp) + interval '1 month')::date) AS payout_from_month
FROM practice_visa_followups f JOIN hr_delivery_bonus_events e ON e.practice_id=f.practice_id
LEFT JOIN hr_visa_bonus_hold_periods h ON h.practice_id=f.practice_id
LEFT JOIN hr_payroll_periods p ON p.id=h.payroll_period_id
WHERE f.fulfilled_at IS NULL AND e.employee_id IS NOT NULL;
-- DROP VIEW lost the read-only grant prod has on it; guarded as in 393
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nuzantara_readonly') THEN
        GRANT SELECT ON TABLE hr_visa_bonus_obligations TO nuzantara_readonly;
    END IF;
END $$;
ALTER TABLE hr_visa_bonus_hold_periods DROP CONSTRAINT ck_hr_visa_hold_one_payout, DROP COLUMN bonus_run_id,
  ALTER COLUMN payroll_period_id SET NOT NULL;
ALTER TABLE hr_bonus_ledger DROP CONSTRAINT ck_hr_bonus_paid_has_payout, DROP CONSTRAINT ck_hr_bonus_one_payout,
  DROP COLUMN bonus_run_id;
CREATE OR REPLACE FUNCTION hr_bonus_run_lock() RETURNS trigger LANGUAGE plpgsql AS $$
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
ALTER TABLE hr_bonus_runs DROP COLUMN paid_by, DROP COLUMN approved_by;
