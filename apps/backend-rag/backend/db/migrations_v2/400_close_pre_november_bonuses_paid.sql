-- depends: 399
-- Bonus reform C, PR3c (owner decision O3, 2026-11-28). Data only, ids and amounts only, no names.
--
-- The approved bonus rows awarded before November 2026 were paid outside the system. They are closed as
-- paid: status 'paid' plus a marker in notes, nothing else. The ledger has no paid_at column and
-- mark_payroll_paid sets none. 399's ck_hr_bonus_paid_has_payout accepts a source-less paid row awarded
-- before 2026-11-01 WITA, so no salary period and no run is invented for them. hr_payroll_bonus_inputs and
-- hr_bonus_run_inputs read only 'pending' and 'approved' rows, so after this no salary and no run can pay
-- them again; the totals that count approved and paid alike do not move.
--
-- The set is matched by PREDICATE and then ASSERTED against the production measurement of 2026-11-28
-- (read-only, scripts/pg.sh): 603 rows, 112410000 IDR, ids 1..644, no notes, no period, no deferral. When
-- the facts differ (another count, another sum, a higher id, a row with notes) the migration gives a NOTICE
-- and changes nothing: a failing release_command would block every later deploy. A correction is a later
-- migration. Only status, notes and updated_at (the ledger's trigger) change; practice_id and employee_id
-- are never in the SET, so the practice and delivery-eligibility triggers do not fire (six of the rows
-- point to a practice that is not completed).
--
-- THIS MIGRATION NEVER WAITS. The allocation lock and the month locks are try-locks raising 55P03, the
-- candidate rows are locked NOWAIT, and run_with_lock_retry retries the whole file. Do not add an EXCEPTION
-- handler: it could swallow the 55P03. Lock order is the HR writers' own: allocation lock, bonus rows, month
-- locks (January to October 2026; they also stop a retroactive salary for those months while this commits).
--
-- Idempotent: once the 603 marked rows are paid, a second run is a NOTICE.
--
-- ROLLBACK, exactly: the rows that still carry the marker and are still paid with no period and no run go
-- back to approved with notes NULL. A marked row that was paid by a period or a run since keeps its state.
-- Roll back 400 before 399 (the rollback reads bonus_run_id).
DO $$
DECLARE
    marker CONSTANT text := 'Closed as paid by migration 400: paid outside the system before November 2026 (owner 2026-11-28)';
    expected_count CONSTANT integer := 603;
    expected_sum CONSTANT bigint := 112410000;
    expected_max_id CONSTANT bigint := 644;
    ids bigint[];
    n integer;
    total bigint;
    max_id bigint;
    with_notes integer;
    closed integer;
    marked integer;
    month_start date;
BEGIN
    IF to_regclass('hr_bonus_ledger') IS NULL THEN
        RAISE NOTICE '400: no bonus ledger here; nothing closed';
        RETURN;
    END IF;
    SELECT count(*) INTO marked FROM hr_bonus_ledger WHERE notes = marker AND status = 'paid';
    IF marked = expected_count THEN
        RAISE NOTICE '400: the % rows are already closed as paid; nothing changed', marked;
        RETURN;
    END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
        RAISE EXCEPTION '400: bonus payout allocation is busy' USING ERRCODE = '55P03';
    END IF;
    SELECT array_agg(id ORDER BY id) INTO ids FROM (
        SELECT id FROM hr_bonus_ledger
         WHERE status = 'approved' AND payroll_period_id IS NULL AND bonus_run_id IS NULL
           AND deferred_from IS NULL
           AND (awarded_at AT TIME ZONE 'Asia/Makassar') < TIMESTAMP '2026-11-01 00:00'
         ORDER BY id
         FOR UPDATE NOWAIT
    ) candidates;
    FOR month_start IN SELECT generate_series(DATE '2026-01-01', DATE '2026-10-01', interval '1 month')::date LOOP
        IF NOT pg_try_advisory_xact_lock(hashtext('hr_payroll_month:' || to_char(month_start, 'YYYY-MM'))) THEN
            RAISE EXCEPTION '400: the % payroll month is busy', to_char(month_start, 'YYYY-MM') USING ERRCODE = '55P03';
        END IF;
    END LOOP;

    SELECT count(*), COALESCE(sum(amount_idr), 0), COALESCE(max(id), 0), count(*) FILTER (WHERE notes IS NOT NULL)
      INTO n, total, max_id, with_notes
      FROM hr_bonus_ledger WHERE id = ANY(COALESCE(ids, ARRAY[]::bigint[]));
    IF n <> expected_count OR total <> expected_sum OR max_id > expected_max_id OR with_notes > 0 THEN
        RAISE NOTICE '400: found % rows, % IDR, max id %, % with notes (expected %, %, <= %, 0); nothing closed',
            n, total, max_id, with_notes, expected_count, expected_sum, expected_max_id;
        RETURN;
    END IF;

    UPDATE hr_bonus_ledger SET status = 'paid', notes = marker
     WHERE id = ANY(ids) AND status = 'approved';
    GET DIAGNOSTICS closed = ROW_COUNT;
    RAISE NOTICE '400: % rows closed as paid, % IDR', closed, total;
END $$;

-- === ROLLBACK ===
-- Same locks in the same order, then only the marked rows come back to approved.
DO $$
DECLARE
    marker CONSTANT text := 'Closed as paid by migration 400: paid outside the system before November 2026 (owner 2026-11-28)';
    month_start date;
BEGIN
    IF to_regclass('hr_bonus_ledger') IS NULL THEN RETURN; END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext('hr_bonus_payout_allocation')) THEN
        RAISE EXCEPTION '400: bonus payout allocation is busy' USING ERRCODE = '55P03';
    END IF;
    PERFORM 1 FROM hr_bonus_ledger WHERE notes = marker ORDER BY id FOR UPDATE NOWAIT;
    FOR month_start IN SELECT generate_series(DATE '2026-01-01', DATE '2026-10-01', interval '1 month')::date LOOP
        IF NOT pg_try_advisory_xact_lock(hashtext('hr_payroll_month:' || to_char(month_start, 'YYYY-MM'))) THEN
            RAISE EXCEPTION '400: the % payroll month is busy', to_char(month_start, 'YYYY-MM') USING ERRCODE = '55P03';
        END IF;
    END LOOP;
    UPDATE hr_bonus_ledger SET status = 'approved', notes = NULL
     WHERE notes = marker AND status = 'paid' AND payroll_period_id IS NULL AND bonus_run_id IS NULL;
END $$;
