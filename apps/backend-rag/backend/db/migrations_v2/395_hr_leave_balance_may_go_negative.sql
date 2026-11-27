-- Annual leave may be taken ahead of the allowance. A balance can then go below zero, and the
-- next year opens with that remainder, plus or minus (hr_service._carry_annual_remainder).
-- Two checks from migration_068 forbid it: carried_over >= 0 and
-- (allocated_days + carried_over) >= (used_days + pending_days). Only those two go;
-- allocated_days, used_days and pending_days stay non-negative.
-- By name first: the names migration_068 gave them, which the rollback below gives back.
ALTER TABLE hr_leave_balances DROP CONSTRAINT IF EXISTS ck_hr_leave_bal_nonneg;
ALTER TABLE hr_leave_balances DROP CONSTRAINT IF EXISTS hr_leave_balances_carried_over_check;

-- The carried_over check was declared on the column, so Postgres named it. A database where
-- either check carries another name is matched by its definition, NOT VALID or not.
DO $$
DECLARE
    found record;
BEGIN
    FOR found IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'hr_leave_balances'::regclass
          AND contype = 'c'
          AND replace(pg_get_constraintdef(oid), ' NOT VALID', '') IN (
              'CHECK ((carried_over >= 0))',
              'CHECK (((allocated_days + carried_over) >= (used_days + pending_days)))'
          )
    LOOP
        EXECUTE format('ALTER TABLE hr_leave_balances DROP CONSTRAINT %I', found.conname);
    END LOOP;
END $$;

-- === ROLLBACK ===
-- NOT VALID: balances already below zero stay as they are; every row written after the
-- rollback is checked again. The names are the ones migration_068 gave both checks.
ALTER TABLE hr_leave_balances
    ADD CONSTRAINT hr_leave_balances_carried_over_check CHECK (carried_over >= 0) NOT VALID;
ALTER TABLE hr_leave_balances
    ADD CONSTRAINT ck_hr_leave_bal_nonneg
    CHECK ((allocated_days + carried_over) >= (used_days + pending_days)) NOT VALID;
