-- Bonus reform B2a (Zero 2026-11-27): a practice completed in November before Final delivery
-- existed earns when its proof is uploaded. The upload path earns every acceptance from the
-- new image on; this explicit data fix records the nine acceptances that happened through the
-- earlier completed-permit restore, measured on production on 2026-11-27 (about 18:20 Bali).
-- One literal row per practice: single-slot ext_b1_voa / ext_c1_tourism, each slot document_1
-- holding one document accepted that day, all assigned to the same team member. Ids only, no
-- names or addresses. The receipt is earned at the slot's updated_at, never at a manufactured time.
-- 1378..1411 are ext_b1_voa with an invoice total equal to the floor (850000): 15000, not
-- forfeited. 1414 is ext_c1_tourism paid 1600000 below its floor 1700000: forfeited, no amount.
-- All earned on the 27th, so the award month equals the earning month (no day-28 roll).
--
-- Each row is inserted only if every measured fact still holds; otherwise it is skipped with a
-- NOTICE and handled by a follow-up explicit migration of the same shape. No ledger insert:
-- payroll still goes through the normal writer or HR. The fixed recorded_at is the marker the
-- rollback uses to find exactly these receipts.
-- THIS MIGRATION NEVER WAITS. Every lock below is NOWAIT: a held lock raises 55P03, the runner
-- (run_with_lock_retry) rolls the attempt back and retries the whole transaction with backoff,
-- and since it never waits it can never be part of a deadlock cycle. Two live writers take
-- opposite orders, so no waiting order is safe for both: confirm_payment locks the practice and
-- then updates the invoice, _persist_invoice upserts the invoice and then updates the practice.
-- Do not add an EXCEPTION handler to this block: it could swallow the 55P03.
DO $$
DECLARE
    marker CONSTANT timestamptz := '2026-11-27 11:00:00+00';
    r record;
    forfeited boolean;
BEGIN
    -- The invoice writer upserts the invoice BEFORE it updates its practice, so the practice
    -- lock below does not serialize it: block invoice writes (reads continue) until we commit.
    LOCK TABLE invoices IN SHARE MODE NOWAIT;
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE recorded_at = marker) THEN
        RAISE EXCEPTION 'The B2a receipt marker is already in use; stop and pick another value';
    END IF;
    FOR r IN
        SELECT * FROM (VALUES
            (1378, 'ext_b1_voa', 21162, '2026-11-27 06:05:30.006241+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1379, 'ext_b1_voa', 21163, '2026-11-27 06:06:04.028734+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1380, 'ext_b1_voa', 21164, '2026-11-27 06:07:02.244355+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1407, 'ext_b1_voa', 21165, '2026-11-27 06:28:28.149941+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1408, 'ext_b1_voa', 21166, '2026-11-27 06:29:24.569338+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1409, 'ext_b1_voa', 21167, '2026-11-27 06:31:02.949951+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1410, 'ext_b1_voa', 21168, '2026-11-27 06:31:48.181301+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1411, 'ext_b1_voa', 21169, '2026-11-27 06:32:26.014731+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 68, 15000, 850000, 850000),
            (1414, 'ext_c1_tourism', 21170, '2026-11-27 06:52:12.022591+00'::timestamptz, 6, 'd76f5df0-d194-415c-86b3-9c3d38ad3cf1', 70, 20000, 1700000, 1600000)
        ) AS t(practice_id, code, document_id, slot_at, employee_id, member_id, rate_id, amount, floor_idr, invoice_total)
    LOOP
        -- The new image is already serving: take the row locks first, in the order the upload
        -- path uses (practice, proof document, then its slots; a permanent document delete locks
        -- the document and then cascades to the slot), so every fact below is read after any
        -- concurrent write has committed and none can change before the insert. The rate, the
        -- employee (hr_bonus_rates.practice_type_code and hr_employees.team_member_id are UNIQUE,
        -- so no second row can appear) and the team member are locked against edits too.
        PERFORM 1 FROM practices WHERE id = r.practice_id FOR UPDATE NOWAIT;
        PERFORM 1 FROM documents WHERE id = r.document_id FOR UPDATE NOWAIT;
        PERFORM 1 FROM practice_delivery_slots WHERE practice_id = r.practice_id FOR UPDATE NOWAIT;
        PERFORM 1 FROM hr_bonus_rates WHERE id = r.rate_id FOR SHARE NOWAIT;
        PERFORM 1 FROM hr_employees WHERE id = r.employee_id FOR SHARE NOWAIT;
        PERFORM 1 FROM team_members WHERE id = r.member_id FOR SHARE NOWAIT;
        IF NOT EXISTS (
            SELECT 1 FROM practices p JOIN practice_types pt ON pt.id = p.practice_type_id
            WHERE p.id = r.practice_id AND p.status = 'completed' AND pt.code = r.code
              AND (p.completion_date AT TIME ZONE 'Asia/Makassar')::date >= DATE '2026-11-01'
              AND (p.completion_date AT TIME ZONE 'Asia/Makassar')::date < DATE '2026-12-01'
        ) THEN
            RAISE NOTICE 'B2a skip %: not a completed % practice of November', r.practice_id, r.code; CONTINUE;
        END IF;
        -- Every practice edit (an assignment change included) and every delivery write logs here.
        IF EXISTS (SELECT 1 FROM activity_log WHERE entity_type = 'practice' AND entity_id = r.practice_id
                   AND performed_at > r.slot_at) THEN
            RAISE NOTICE 'B2a skip %: the practice was changed after the measured acceptance', r.practice_id; CONTINUE;
        END IF;
        IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE practice_id = r.practice_id)
           OR EXISTS (SELECT 1 FROM hr_bonus_ledger WHERE practice_id = r.practice_id)
           OR EXISTS (SELECT 1 FROM practice_delivery_completions WHERE practice_id = r.practice_id) THEN
            RAISE NOTICE 'B2a skip %: already has a receipt, a ledger row or a completion', r.practice_id; CONTINUE;
        END IF;
        IF (SELECT count(*) FROM practice_delivery_slots WHERE practice_id = r.practice_id) <> 1
           OR NOT EXISTS (
            SELECT 1 FROM practice_delivery_slots s JOIN practices p ON p.id = s.practice_id
            JOIN documents d ON d.id = s.document_id
            WHERE s.practice_id = r.practice_id AND s.service_code = r.code AND s.slot_key = 'document_1'
              AND s.document_id = r.document_id AND s.updated_at = r.slot_at
              AND d.practice_id = p.id AND d.client_id = p.client_id
              AND d.family_member_id IS NOT DISTINCT FROM p.family_member_id
              AND NULLIF(d.file_id, '') IS NOT NULL AND d.deleted_at IS NULL
              AND NOT COALESCE(d.is_archived, false) AND d.status IS DISTINCT FROM 'deleted'
        ) THEN
            RAISE NOTICE 'B2a skip %: the slot or its document changed since it was measured', r.practice_id; CONTINUE;
        END IF;
        IF (SELECT sum(amount_idr) FROM invoices WHERE practice_id = r.practice_id) IS DISTINCT FROM r.invoice_total THEN
            RAISE NOTICE 'B2a skip %: the invoice total changed', r.practice_id; CONTINUE;
        END IF;
        -- The assignee resolves to the measured team member and employee (by id, never by address).
        IF (SELECT count(*) FROM team_members tm JOIN practices p
                ON lower(trim(tm.email)) = NULLIF(lower(trim(p.assigned_to)), '')
            WHERE p.id = r.practice_id) <> 1
           OR NOT EXISTS (
            SELECT 1 FROM practices p JOIN team_members tm ON lower(trim(tm.email)) = NULLIF(lower(trim(p.assigned_to)), '')
            JOIN hr_employees e ON e.team_member_id = tm.id
            WHERE p.id = r.practice_id AND tm.id = r.member_id AND e.id = r.employee_id
        ) THEN
            RAISE NOTICE 'B2a skip %: the assignee is not the measured team member', r.practice_id; CONTINUE;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM hr_bonus_rates WHERE id = r.rate_id AND practice_type_code = r.code AND is_active
              AND amount_idr = r.amount AND forfeit_below_idr = r.floor_idr
        ) THEN
            RAISE NOTICE 'B2a skip %: the rate changed', r.practice_id; CONTINUE;
        END IF;
        forfeited := r.invoice_total < r.floor_idr;
        INSERT INTO hr_delivery_bonus_events
            (practice_id, employee_id, original_team_member_id, payroll_ineligible, service_code, event_kind,
             earned_at, recorded_at, awarded_at, bonus_rate_id, amount_idr, ledger_id, forfeited)
        VALUES (r.practice_id, r.employee_id, r.member_id, FALSE, r.code, 'service_delivered',
                r.slot_at, marker, r.slot_at, r.rate_id, CASE WHEN forfeited THEN NULL ELSE r.amount END, NULL, forfeited);
    END LOOP;
END $$;

-- === ROLLBACK ===
-- Removes exactly the receipts this file inserted (listed practice, fixed marker) in one
-- transaction, and refuses when any of them was booked since: roll forward from there.
DO $$
DECLARE
    marker CONSTANT timestamptz := '2026-11-27 11:00:00+00';
    ours integer[] := ARRAY[1378, 1379, 1380, 1407, 1408, 1409, 1410, 1411, 1414];
BEGIN
    PERFORM 1 FROM hr_delivery_bonus_events WHERE practice_id = ANY(ours) AND recorded_at = marker FOR UPDATE;
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events
               WHERE practice_id = ANY(ours) AND recorded_at = marker AND ledger_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Preserve booked B2a receipts before rolling back migration 391';
    END IF;
    DELETE FROM hr_delivery_bonus_events WHERE practice_id = ANY(ours) AND recorded_at = marker;
END $$;
