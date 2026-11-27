-- Bonus reform C, PR2b (owner rule 2026-11-27). Data only, two effects, ids and amounts only.
--
-- EFFECT 1, the bonuses the paid November salary already carried: ledger rows 645-649 (employee 4: 645,
-- 646, 647, 648 = 450000; employee 3: 649 = 200000) were on the November payslips and paid with that
-- salary (period 44, paid 2026-11-18), but stayed 'approved' with no period because approve_payroll
-- reserved nothing before #789. They are set to the state mark_payroll_paid would have left:
-- payroll_period_id = 44, status = 'paid'. The ledger has no paid_at column, and mark_payroll_paid
-- sets none. Nothing else on these rows changes. Done FIRST, so the lock order is the writers' own:
-- allocation lock, ledger rows, month lock, period row.
--
-- EFFECT 2, the November backlog: the November 2026 receipts the paid November salary left unbooked
-- are booked as pending into their own bonus month, exactly as _write_ledger books one
-- (delivery_bonus.py:46-70): same employee, code, rate, amount and awarded_at; the only addition is
-- the marker in notes.
--
-- Applied after 393's image is live everywhere: no writer refuses November any more, so nothing can get
-- stuck after this commits (no release-window stragglers).
-- THIS MIGRATION NEVER WAITS. Row locks are NOWAIT and the advisory locks are try-locks raising 55P03,
-- which run_with_lock_retry retries as a whole (lock_timeout 2s, up to 8 attempts). Do not add an
-- EXCEPTION handler: it could swallow the 55P03.
--
-- ROLLBACK promise, exactly: effect 2 removes the rows that STILL carry the marker in notes and are
-- still pending. A row HR rejected with a note of its own (reject_bonus overwrites notes) is no longer
-- recognised: it keeps its rejection and its receipt link, and no money moves. Effect 1 restores
-- exactly rows 645-649 to approved, and refuses if any of them changed since.
-- depends: 393

-- === EFFECT 1 ===
DO $$
DECLARE
    ids CONSTANT bigint[] := ARRAY[645, 646, 647, 648, 649];
    november_period CONSTANT integer := 44;
    matched integer;
    already integer;
    mismatch integer;
BEGIN
    IF to_regclass('hr_bonus_ledger') IS NULL OR to_regclass('hr_payroll_periods') IS NULL
       OR to_regclass('hr_payslips') IS NULL THEN
        RAISE NOTICE '394: no payroll tables here; nothing corrected';
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM hr_bonus_ledger WHERE id = ANY(ids)) THEN
        RAISE NOTICE '394: rows 645-649 do not exist here; nothing corrected';
        RETURN;
    END IF;
    -- Order of the HR writers (hr_service.py): allocation lock, bonus rows, month lock, period row.
    -- mark_payroll_paid holds the allocation lock exclusive, and this change is its twin.
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
        RAISE EXCEPTION '394: bonus payout allocation is busy' USING ERRCODE = '55P03';
    END IF;
    PERFORM 1 FROM hr_bonus_ledger WHERE id = ANY(ids) ORDER BY id FOR UPDATE NOWAIT;
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_payroll_month:2026-11')) THEN
        RAISE EXCEPTION '394: the November 2026 payroll month is busy' USING ERRCODE = '55P03';
    END IF;
    PERFORM 1 FROM hr_payroll_periods WHERE id = november_period FOR SHARE NOWAIT;

    SELECT count(*) INTO already FROM hr_bonus_ledger
     WHERE id = ANY(ids) AND status = 'paid' AND payroll_period_id = november_period;
    IF already = cardinality(ids) THEN
        RAISE NOTICE '394: rows 645-649 are already paid on period 44; nothing corrected';
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM hr_payroll_periods
                   WHERE id = november_period AND payroll_year = 2026 AND payroll_month = 11
                     AND status = 'paid') THEN
        RAISE NOTICE '394: period 44 is not the paid November 2026 salary; nothing corrected';
        RETURN;
    END IF;
    SELECT count(*) INTO matched FROM (VALUES
        (645, 4, 100000), (646, 4, 100000), (647, 4, 100000), (648, 4, 150000), (649, 3, 200000)
    ) AS t(id, employee_id, amount_idr)
    JOIN hr_bonus_ledger b ON b.id = t.id AND b.employee_id = t.employee_id
                          AND b.amount_idr = t.amount_idr
    WHERE b.status = 'approved' AND b.payroll_period_id IS NULL AND b.deferred_from IS NULL
      AND (b.awarded_at AT TIME ZONE 'Asia/Makassar') >= TIMESTAMP '2026-11-01 00:00'
      AND (b.awarded_at AT TIME ZONE 'Asia/Makassar') <  TIMESTAMP '2026-12-01 00:00';
    IF matched <> cardinality(ids) THEN
        RAISE NOTICE '394: rows 645-649 do not all match the measured facts; nothing corrected';
        RETURN;
    END IF;
    -- Per employee: the one November slip (UNIQUE per employee and period; none gives a NULL sum) carried
    -- exactly these rows, and nothing else is reserved to the period for that employee.
    SELECT count(*) INTO mismatch FROM (VALUES (4, 450000), (3, 200000)) AS t(employee_id, bonus)
    WHERE (SELECT sum(p.bonus_total_idr) FROM hr_payslips p
            WHERE p.payroll_period_id = november_period AND p.employee_id = t.employee_id)
              IS DISTINCT FROM t.bonus
       OR EXISTS (SELECT 1 FROM hr_bonus_ledger o
                   WHERE o.payroll_period_id = november_period AND o.employee_id = t.employee_id
                     AND NOT (o.id = ANY(ids)));
    IF mismatch > 0 THEN
        RAISE NOTICE '394: the November payslips do not match rows 645-649; nothing corrected';
        RETURN;
    END IF;
    UPDATE hr_bonus_ledger SET payroll_period_id = november_period, status = 'paid'
     WHERE id = ANY(ids) AND status = 'approved' AND payroll_period_id IS NULL;
    RAISE NOTICE '394: rows 645-649 recorded as paid on period 44';
END $$;

-- === EFFECT 2 ===
DO $$
DECLARE
    marker CONSTANT text := 'Booked by migration 394 into the November 2026 bonus month';
    ids integer[];
    r record;
    new_ledger bigint;
    booked integer := 0;
BEGIN
    IF to_regclass('hr_delivery_bonus_events') IS NULL OR to_regclass('hr_bonus_ledger') IS NULL
       OR to_regclass('practices') IS NULL OR to_regclass('hr_employees') IS NULL
       OR to_regclass('hr_bonus_rates') IS NULL OR to_regclass('hr_bonus_runs') IS NULL THEN
        RAISE NOTICE '394: no bonus ledger here; nothing booked';
        RETURN;
    END IF;
    -- Order as resolve_delivery_bonus: receipt rows, the month lock, then the parent rows.
    SELECT array_agg(practice_id ORDER BY practice_id) INTO ids FROM (
        SELECT e.practice_id FROM hr_delivery_bonus_events e
        WHERE e.ledger_id IS NULL AND e.employee_id IS NOT NULL AND e.amount_idr > 0
          AND NOT e.forfeited AND NOT e.payroll_ineligible
          AND (e.awarded_at AT TIME ZONE 'Asia/Makassar') >= TIMESTAMP '2026-11-01 00:00'
          AND (e.awarded_at AT TIME ZONE 'Asia/Makassar') <  TIMESTAMP '2026-12-01 00:00'
        ORDER BY e.practice_id
        FOR UPDATE OF e NOWAIT
    ) candidates;
    IF ids IS NULL THEN
        RAISE NOTICE '394: no stuck November receipt; nothing booked';
        RETURN;
    END IF;
    IF NOT pg_try_advisory_xact_lock_shared(hashtext('hr_bonus_month:2026-11')) THEN
        RAISE EXCEPTION '394: the November 2026 bonus month is being closed' USING ERRCODE = '55P03';
    END IF;
    IF hr_bonus_month_closed(DATE '2026-11-01') THEN
        RAISE NOTICE '394: the November 2026 bonus run is closed; nothing booked';
        RETURN;
    END IF;
    -- Parents read by the ledger triggers and foreign keys: frozen until commit, never waited for.
    -- practices FOR SHARE also blocks a status change between the guard below and the insert trigger.
    PERFORM 1 FROM practices WHERE id = ANY(ids) ORDER BY id FOR SHARE NOWAIT;
    PERFORM 1 FROM hr_employees WHERE id IN (
        SELECT employee_id FROM hr_delivery_bonus_events WHERE practice_id = ANY(ids)) ORDER BY id FOR KEY SHARE NOWAIT;
    PERFORM 1 FROM hr_bonus_rates WHERE id IN (
        SELECT bonus_rate_id FROM hr_delivery_bonus_events WHERE practice_id = ANY(ids)) ORDER BY id FOR KEY SHARE NOWAIT;
    FOR r IN
        SELECT e.practice_id, e.employee_id, e.service_code, e.bonus_rate_id, e.amount_idr, e.awarded_at,
               e.event_kind, p.status AS practice_status
        FROM hr_delivery_bonus_events e JOIN practices p ON p.id = e.practice_id
        WHERE e.practice_id = ANY(ids) ORDER BY e.practice_id
    LOOP
        -- hr_validate_completed_practice (384): a service bonus needs a completed practice; the
        -- e-visa and IMK milestones earn before completion (same employee and code by construction).
        IF r.event_kind NOT IN ('electronic_visa_issued', 'kitas_imk_uploaded')
           AND r.practice_status IS DISTINCT FROM 'completed' THEN
            RAISE NOTICE '394 skip %: the practice is no longer completed', r.practice_id; CONTINUE;
        END IF;
        -- uq_hr_bonus_practice_emp and the writer's legacy adoption: never a second row for a practice.
        IF EXISTS (SELECT 1 FROM hr_bonus_ledger WHERE practice_id = r.practice_id) THEN
            RAISE NOTICE '394 skip %: the practice already has a ledger row', r.practice_id; CONTINUE;
        END IF;
        INSERT INTO hr_bonus_ledger
            (practice_id, employee_id, practice_type_code, bonus_rate_id, amount_idr, awarded_at, status, notes)
        VALUES (r.practice_id, r.employee_id, r.service_code, r.bonus_rate_id, r.amount_idr, r.awarded_at,
                'pending', marker)
        RETURNING id INTO new_ledger;
        UPDATE hr_delivery_bonus_events SET ledger_id = new_ledger WHERE practice_id = r.practice_id;
        booked := booked + 1;
    END LOOP;
    RAISE NOTICE '394: booked % November receipts', booked;
END $$;

-- === ROLLBACK ===
-- Effect 2 first (reverse order), then effect 1; both NOWAIT, in the forward lock order. Effect 2 removes
-- exactly the rows that still carry the marker and unlinks their receipts; it refuses when any of them
-- moved on since (roll forward from there). A row HR rejected with its own note is not recognised.
DO $$
DECLARE
    marker CONSTANT text := 'Booked by migration 394 into the November 2026 bonus month';
BEGIN
    IF to_regclass('hr_bonus_ledger') IS NULL OR to_regclass('hr_delivery_bonus_events') IS NULL THEN RETURN; END IF;
    PERFORM 1 FROM hr_delivery_bonus_events e JOIN hr_bonus_ledger b ON b.id = e.ledger_id
     WHERE b.notes = marker ORDER BY e.practice_id FOR UPDATE OF e NOWAIT;
    PERFORM 1 FROM hr_bonus_ledger WHERE notes = marker ORDER BY id FOR UPDATE NOWAIT;
    IF EXISTS (SELECT 1 FROM hr_bonus_ledger b WHERE b.notes = marker
               AND (b.status <> 'pending' OR b.payroll_period_id IS NOT NULL OR b.deferred_from IS NOT NULL
                    OR EXISTS (SELECT 1 FROM hr_bonus_deferral_causes c WHERE c.bonus_id = b.id))) THEN
        RAISE EXCEPTION 'Preserve November bonuses approved, rejected or reserved since migration 394';
    END IF;
    UPDATE hr_delivery_bonus_events SET ledger_id = NULL
     WHERE ledger_id IN (SELECT id FROM hr_bonus_ledger WHERE notes = marker);
    DELETE FROM hr_bonus_ledger WHERE notes = marker;
END $$;

DO $$
DECLARE
    ids CONSTANT bigint[] := ARRAY[645, 646, 647, 648, 649];
    present integer;
BEGIN
    IF to_regclass('hr_bonus_ledger') IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO present FROM hr_bonus_ledger WHERE id = ANY(ids);
    IF present = 0 THEN RETURN; END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
        RAISE EXCEPTION '394: bonus payout allocation is busy' USING ERRCODE = '55P03';
    END IF;
    PERFORM 1 FROM hr_bonus_ledger WHERE id = ANY(ids) ORDER BY id FOR UPDATE NOWAIT;
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_payroll_month:2026-11')) THEN
        RAISE EXCEPTION '394: the November 2026 payroll month is busy' USING ERRCODE = '55P03';
    END IF;
    -- Either still as 394 left them (paid on period 44) or already as before it; anything else moved on.
    IF present <> cardinality(ids) OR EXISTS (
        SELECT 1 FROM hr_bonus_ledger
         WHERE id = ANY(ids)
           AND NOT ((status = 'paid' AND payroll_period_id = 44)
                    OR (status = 'approved' AND payroll_period_id IS NULL))) THEN
        RAISE EXCEPTION 'Preserve bonus rows 645-649 changed since migration 394';
    END IF;
    UPDATE hr_bonus_ledger SET status = 'approved', payroll_period_id = NULL
     WHERE id = ANY(ids) AND status = 'paid' AND payroll_period_id = 44;
END $$;
