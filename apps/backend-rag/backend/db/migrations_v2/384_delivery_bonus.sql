-- A delivery bonus belongs to one qualifying event and its original assignee.
-- Rates may be absent at issue time. Preserve the receipt without putting a
-- fictional zero in payroll. A superseded document keeps its first receipt.
CREATE TABLE hr_delivery_bonus_events (
    practice_id INTEGER PRIMARY KEY REFERENCES practices(id) ON DELETE RESTRICT,
    employee_id INTEGER REFERENCES hr_employees(id) ON DELETE RESTRICT,
    service_code TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('electronic_visa_issued', 'service_delivered')),
    earned_at TIMESTAMPTZ NOT NULL,
    awarded_at TIMESTAMPTZ NOT NULL,
    bonus_rate_id INTEGER REFERENCES hr_bonus_rates(id) ON DELETE SET NULL,
    amount_idr BIGINT CHECK (amount_idr >= 0),
    ledger_id BIGINT UNIQUE REFERENCES hr_bonus_ledger(id) ON DELETE RESTRICT,
    resolved_by TEXT,
    CHECK (ledger_id IS NULL OR (employee_id IS NOT NULL AND amount_idr IS NOT NULL))
);
CREATE TABLE hr_delivery_goals (
    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE CASCADE,
    month DATE NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),
    target INTEGER NOT NULL CHECK (target BETWEEN 1 AND 200),
    PRIMARY KEY (employee_id, month)
);
CREATE INDEX idx_hr_delivery_events_employee ON hr_delivery_bonus_events (employee_id, earned_at DESC);

CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.practice_id IS NULL OR EXISTS (
        SELECT 1 FROM practices WHERE id = NEW.practice_id AND status = 'completed'
    ) OR EXISTS (
        SELECT 1 FROM hr_delivery_bonus_events e
        WHERE e.practice_id = NEW.practice_id AND e.employee_id = NEW.employee_id
          AND e.service_code = NEW.practice_type_code AND e.event_kind = 'electronic_visa_issued'
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'A practice bonus requires completion or a recorded electronic visa issuance';
END;
$$ LANGUAGE plpgsql;

-- === ROLLBACK ===
-- Unresolved bonus claims need to be retained. Refuse a rollback that loses them.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events) THEN
        RAISE EXCEPTION 'Preserve earned bonus events before rolling back this migration';
    END IF;
END $$;
CREATE OR REPLACE FUNCTION hr_validate_completed_practice()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.practice_id IS NULL OR EXISTS (
        SELECT 1 FROM practices WHERE id = NEW.practice_id AND status = 'completed'
    ) THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'Practice must be completed before bonus entry';
END;
$$ LANGUAGE plpgsql;
DROP TABLE hr_delivery_goals;
DROP TABLE hr_delivery_bonus_events;
