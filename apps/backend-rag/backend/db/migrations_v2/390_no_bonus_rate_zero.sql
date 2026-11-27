-- Bonus reform (Zero 2026-11-27): a rate of 0 means the service carries no bonus. The
-- delivery receipt stays visible to the employee as "no bonus" and never becomes a payroll
-- line, so a booked receipt always carries a positive amount.
ALTER TABLE hr_delivery_bonus_events ADD CONSTRAINT hr_delivery_bonus_events_booked_positive
    CHECK (ledger_id IS NULL OR amount_idr > 0);

-- other_custom carries no bonus from November 2026.
INSERT INTO hr_bonus_rates (practice_type_code, amount_idr, forfeit_below_idr, effective_from, is_active, notes)
VALUES ('other_custom', 0, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: no bonus')
ON CONFLICT (practice_type_code) DO UPDATE SET
    amount_idr = EXCLUDED.amount_idr, forfeit_below_idr = NULL, effective_from = EXCLUDED.effective_from,
    is_active = TRUE, effective_to = NULL, notes = EXCLUDED.notes, updated_at = now();

-- Its unpriced, unbooked receipts earned since then read as "no bonus" instead of waiting for HR.
UPDATE hr_delivery_bonus_events e
SET bonus_rate_id = r.id, amount_idr = 0
FROM hr_bonus_rates r
WHERE e.service_code = 'other_custom' AND e.amount_idr IS NULL AND e.ledger_id IS NULL
  AND NOT e.payroll_ineligible AND NOT e.forfeited AND e.resolved_by IS NULL
  AND r.practice_type_code = 'other_custom' AND r.is_active
  AND r.effective_from <= (e.earned_at AT TIME ZONE 'Asia/Makassar')::date;

-- === ROLLBACK ===
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE amount_idr = 0) THEN
        RAISE EXCEPTION 'Preserve no-bonus receipts before rolling back this migration';
    END IF;
END $$;
DELETE FROM hr_bonus_rates WHERE practice_type_code = 'other_custom' AND amount_idr = 0;
ALTER TABLE hr_delivery_bonus_events DROP CONSTRAINT hr_delivery_bonus_events_booked_positive;
