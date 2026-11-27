-- Owner-approved bonus amounts (2026-11-27) and the forfeit rule for visa extensions:
-- when the client paid less than the service price, the bonus is forfeited. The invoice
-- amount is already net of discount, so the comparison needs no further subtraction.
ALTER TABLE hr_bonus_rates ADD COLUMN forfeit_below_idr BIGINT CHECK (forfeit_below_idr > 0);
-- A forfeited event is terminal: it never carries an amount or a ledger row, so payroll
-- cannot see it and no fictional zero is written.
ALTER TABLE hr_delivery_bonus_events ADD COLUMN forfeited BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hr_delivery_bonus_events ADD CONSTRAINT hr_delivery_bonus_events_forfeited_clean
    CHECK (NOT forfeited OR (ledger_id IS NULL AND amount_idr IS NULL));

-- True when the practice was paid below p_price, NULL when that is unknown. plpgsql keeps
-- the body unvalidated at creation, so the invoices table is only needed at call time.
CREATE FUNCTION hr_practice_paid_below(p_practice_id integer, p_price bigint)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE net bigint;
BEGIN
    SELECT SUM(amount_idr) INTO net FROM invoices WHERE practice_id = p_practice_id;
    IF net IS NULL THEN
        SELECT quoted_price - COALESCE(discount_amount, 0) INTO net FROM practices WHERE id = p_practice_id;
    END IF;
    RETURN net < p_price;
END;
$$;

INSERT INTO hr_bonus_rates (practice_type_code, amount_idr, forfeit_below_idr, effective_from, is_active, notes)
VALUES
    ('ext_b1_voa', 15000, 850000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('voa_extension', 15000, 850000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('ext_c1_tourism', 20000, 1700000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('other_mutation_passport', 10000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('visa_bridging', 100000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('visa_b1_voa', 20000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_efin_application', 100000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_update_data_coretax', 100000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_close_pma_company', 1500000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_npwpd_registration', 500000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_bpjs_employee', 150000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_bpjs_insurance', 300000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27'),
    ('tax_annual_company_zero', 500000, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27')
ON CONFLICT (practice_type_code) DO UPDATE SET
    amount_idr = EXCLUDED.amount_idr, forfeit_below_idr = EXCLUDED.forfeit_below_idr,
    is_active = TRUE, effective_to = NULL, notes = EXCLUDED.notes, updated_at = now();

-- Price the receipts that were kept unpriced because no rate existed, and forfeit the
-- below-price ones. Resolved, booked and excluded receipts are untouched; no ledger rows
-- are written here: payroll months are not this migration's to open.
UPDATE hr_delivery_bonus_events e
SET bonus_rate_id = r.id,
    amount_idr = CASE WHEN r.forfeit_below_idr IS NULL
                           OR hr_practice_paid_below(e.practice_id, r.forfeit_below_idr) IS FALSE
                      THEN r.amount_idr END,
    forfeited = COALESCE(r.forfeit_below_idr IS NOT NULL
                         AND hr_practice_paid_below(e.practice_id, r.forfeit_below_idr), FALSE)
FROM hr_bonus_rates r
WHERE e.amount_idr IS NULL AND e.ledger_id IS NULL AND NOT e.payroll_ineligible
  AND e.resolved_by IS NULL AND NOT e.forfeited
  AND r.practice_type_code = e.service_code AND r.is_active
  AND r.effective_from <= (e.earned_at AT TIME ZONE 'Asia/Makassar')::date
  AND (r.effective_to IS NULL OR r.effective_to >= (e.earned_at AT TIME ZONE 'Asia/Makassar')::date);

-- === ROLLBACK ===
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE forfeited) THEN
        RAISE EXCEPTION 'Preserve forfeited bonus events before rolling back this migration';
    END IF;
END $$;
ALTER TABLE hr_delivery_bonus_events DROP CONSTRAINT hr_delivery_bonus_events_forfeited_clean;
ALTER TABLE hr_delivery_bonus_events DROP COLUMN forfeited;
DROP FUNCTION hr_practice_paid_below(integer, bigint);
ALTER TABLE hr_bonus_rates DROP COLUMN forfeit_below_idr;
