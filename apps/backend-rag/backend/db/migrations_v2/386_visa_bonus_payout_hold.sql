-- Keep the document's date distinct from the timestamp at which the upload earns a bonus.
-- Existing, already recorded awards retain both their payroll month and original earned date.
ALTER TABLE hr_delivery_bonus_events ADD COLUMN evidence_issued_on DATE;
UPDATE hr_delivery_bonus_events SET evidence_issued_on=(earned_at AT TIME ZONE 'Asia/Makassar')::date
WHERE event_kind='electronic_visa_issued';

ALTER TABLE hr_delivery_bonus_events DROP CONSTRAINT hr_delivery_bonus_events_event_kind_check;
ALTER TABLE hr_delivery_bonus_events ADD CONSTRAINT hr_delivery_bonus_events_event_kind_check
    CHECK (event_kind IN ('electronic_visa_issued','kitas_imk_uploaded','service_delivered'));

CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.practice_id IS NULL OR EXISTS (
        SELECT 1 FROM practices WHERE id=NEW.practice_id AND status='completed'
    ) OR EXISTS (
        SELECT 1 FROM hr_delivery_bonus_events e
        WHERE e.practice_id=NEW.practice_id AND e.employee_id=NEW.employee_id
          AND e.service_code=NEW.practice_type_code
          AND e.event_kind IN ('electronic_visa_issued','kitas_imk_uploaded')
    ) THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'A practice bonus requires completion or the qualifying visa or IMK upload';
END;
$$ LANGUAGE plpgsql;

-- The first approved payroll that had to withhold a bonus. The earning date and amount
-- stay on the original ledger entry. payroll_period_id later reserves its one actual payout.
ALTER TABLE hr_bonus_ledger ADD COLUMN deferred_from DATE;
CREATE INDEX ix_hr_bonus_unallocated_carry ON hr_bonus_ledger (employee_id, deferred_from)
    WHERE deferred_from IS NOT NULL AND payroll_period_id IS NULL;

-- Pin the first affected payroll even if the consultant earned nothing that month.
-- The individual withheld rows keep all contributing cases, so overlapping holds
-- release independently and one missing permit cannot freeze later earnings again.
CREATE TABLE hr_visa_bonus_hold_periods (
    practice_id INTEGER PRIMARY KEY REFERENCES practice_visa_followups(practice_id) ON DELETE RESTRICT,
    payroll_period_id INTEGER NOT NULL REFERENCES hr_payroll_periods(id) ON DELETE RESTRICT
);
CREATE TABLE hr_bonus_deferral_causes (
    bonus_id BIGINT NOT NULL REFERENCES hr_bonus_ledger(id) ON DELETE CASCADE,
    practice_id INTEGER NOT NULL REFERENCES hr_visa_bonus_hold_periods(practice_id) ON DELETE RESTRICT,
    PRIMARY KEY (bonus_id,practice_id)
);

CREATE VIEW hr_visa_bonus_obligations AS
SELECT f.practice_id, e.employee_id, f.service_code, f.slot_key, f.started_on,
       f.due_on, f.email_state, h.payroll_period_id AS bound_period_id, p.period_start AS bound_month,
       GREATEST(date_trunc('month', f.due_on::timestamp)::date,
                (date_trunc('month', f.started_on::timestamp) + interval '1 month')::date) AS payout_from_month
FROM practice_visa_followups f JOIN hr_delivery_bonus_events e ON e.practice_id=f.practice_id
LEFT JOIN hr_visa_bonus_hold_periods h ON h.practice_id=f.practice_id
LEFT JOIN hr_payroll_periods p ON p.id=h.payroll_period_id
WHERE f.fulfilled_at IS NULL AND f.owner_release_at IS NULL AND e.employee_id IS NOT NULL;

CREATE FUNCTION hr_visa_holds_for_period(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (practice_id INTEGER,employee_id INTEGER)
LANGUAGE SQL STABLE AS $$
    SELECT o.practice_id,o.employee_id FROM hr_visa_bonus_obligations o
    WHERE o.due_on <= as_of AND o.payout_from_month <= target_month AND (
        o.bound_period_id=target_period OR (o.bound_period_id IS NULL AND NOT EXISTS (
             SELECT 1 FROM hr_payroll_periods earlier WHERE earlier.status IN ('draft','calculated')
               AND earlier.period_start >= o.payout_from_month AND earlier.period_start < target_month
        ))
    )
$$;

-- Calculation and approval use the same inputs. Only deliberately deferred bonuses can
-- cross a month boundary. Closed legacy months are never rediscovered as new earnings.
CREATE FUNCTION hr_payroll_bonus_inputs(target_month DATE, target_period INTEGER, as_of DATE)
RETURNS TABLE (bonus_id BIGINT, employee_id INTEGER, amount_idr BIGINT, status TEXT, held BOOLEAN)
LANGUAGE SQL STABLE AS $$
    SELECT b.id, b.employee_id, b.amount_idr, b.status::text,
           EXISTS (SELECT 1 FROM hr_visa_holds_for_period(target_month,target_period,as_of) h
                   WHERE h.employee_id=b.employee_id)
           OR EXISTS (
               SELECT 1 FROM hr_bonus_deferral_causes c JOIN hr_visa_bonus_obligations o USING(practice_id)
                WHERE c.bonus_id=b.id AND o.due_on <= as_of
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

-- === ROLLBACK ===
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM hr_visa_bonus_hold_periods) OR EXISTS(SELECT 1 FROM hr_bonus_ledger WHERE deferred_from IS NOT NULL)
       OR EXISTS(SELECT 1 FROM hr_delivery_bonus_events WHERE event_kind='kitas_imk_uploaded'
                 OR evidence_issued_on IS DISTINCT FROM (earned_at AT TIME ZONE 'Asia/Makassar')::date
                    AND evidence_issued_on IS NOT NULL) THEN
        RAISE EXCEPTION 'Preserve earned bonus evidence and deferred payouts before rollback';
    END IF;
END $$;
DROP FUNCTION hr_payroll_bonus_inputs(DATE,INTEGER,DATE);
DROP FUNCTION hr_visa_holds_for_period(DATE,INTEGER,DATE);
DROP VIEW hr_visa_bonus_obligations;
DROP TABLE hr_bonus_deferral_causes;
DROP TABLE hr_visa_bonus_hold_periods;
ALTER TABLE hr_bonus_ledger DROP COLUMN deferred_from;
ALTER TABLE hr_delivery_bonus_events DROP COLUMN evidence_issued_on;
ALTER TABLE hr_delivery_bonus_events DROP CONSTRAINT hr_delivery_bonus_events_event_kind_check;
ALTER TABLE hr_delivery_bonus_events ADD CONSTRAINT hr_delivery_bonus_events_event_kind_check
    CHECK (event_kind IN ('electronic_visa_issued','service_delivered'));

CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.practice_id IS NULL OR EXISTS (
        SELECT 1 FROM practices WHERE id=NEW.practice_id AND status='completed'
    ) OR EXISTS (
        SELECT 1 FROM hr_delivery_bonus_events e WHERE e.practice_id=NEW.practice_id
          AND e.employee_id=NEW.employee_id AND e.service_code=NEW.practice_type_code
          AND e.event_kind='electronic_visa_issued'
    ) THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'A practice bonus requires completion or a recorded electronic visa issuance';
END;
$$ LANGUAGE plpgsql;
