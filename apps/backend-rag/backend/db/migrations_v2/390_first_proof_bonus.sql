-- Bonus reform point B (Zero 2026-11-27): the first proof uploaded in Final delivery completes
-- the practice and earns its bonus; a rate of 0 means the service carries no bonus. A no-bonus
-- receipt stays visible to the employee and never becomes a payroll line.
INSERT INTO hr_bonus_rates (practice_type_code, amount_idr, forfeit_below_idr, effective_from, is_active, notes)
VALUES ('other_custom', 0, NULL, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: no bonus')
ON CONFLICT (practice_type_code) DO UPDATE SET
    amount_idr = EXCLUDED.amount_idr, forfeit_below_idr = NULL,
    is_active = TRUE, effective_to = NULL, notes = EXCLUDED.notes, updated_at = now();

UPDATE hr_delivery_bonus_events e
SET bonus_rate_id = r.id, amount_idr = 0
FROM hr_bonus_rates r
WHERE e.service_code = 'other_custom' AND e.amount_idr IS NULL AND e.ledger_id IS NULL
  AND NOT e.payroll_ineligible AND NOT e.forfeited AND e.resolved_by IS NULL
  AND r.practice_type_code = 'other_custom' AND r.is_active
  AND r.effective_from <= (e.earned_at AT TIME ZONE 'Asia/Makassar')::date;

-- A November practice completed before Final delivery earns when its proof is uploaded, at the
-- upload time. Until now a completed practice accepted only #982's permit repairs, which earned
-- nothing; these are the services whose repaired permit is their proof (delivery_catalog.csv).
-- The beneficiary, rate and forfeit rules are record_delivery_bonus's, with the payroll set
-- predicate of hr_service.payroll_set_sql() as of this migration. No ledger row is written:
-- payroll months are not this migration's to open.
WITH proof AS (
    SELECT p.id AS practice_id, pt.code, p.assigned_to, min(s.updated_at) AS earned_at,
           CASE WHEN pt.code LIKE 'kitas\_%\_extend' THEN 'kitas_imk_uploaded'
                ELSE 'service_delivered' END AS kind
    FROM practices p
    JOIN practice_types pt ON pt.id = p.practice_type_id
    JOIN practice_delivery_slots s ON s.practice_id = p.id AND s.service_code = pt.code
         AND s.document_id IS NOT NULL
    WHERE p.status = 'completed'
      AND (p.completion_date AT TIME ZONE 'Asia/Makassar')::date >= DATE '2026-11-01'
      AND NOT EXISTS (SELECT 1 FROM practice_delivery_completions c WHERE c.practice_id = p.id)
      AND NOT EXISTS (SELECT 1 FROM hr_delivery_bonus_events e WHERE e.practice_id = p.id)
      AND (pt.code IN ('ext_b1_voa', 'voa_extension', 'ext_c1_tourism', 'kitas_born',
                       'kitap_dependent_merp', 'kitap_investor_merp', 'kitap_retirement_merp')
           OR (pt.code LIKE 'kitas\_%\_extend' AND s.slot_key = 'document_1'))
    GROUP BY p.id, pt.code
)
INSERT INTO hr_delivery_bonus_events
    (practice_id, employee_id, service_code, event_kind, earned_at, awarded_at,
     bonus_rate_id, amount_idr, ledger_id, recorded_at, original_team_member_id,
     payroll_ineligible, forfeited)
SELECT proof.practice_id, COALESCE(old.employee_id, employee.id), proof.code, proof.kind,
       proof.earned_at,
       COALESCE(old.awarded_at, CASE
           WHEN EXTRACT(DAY FROM proof.earned_at AT TIME ZONE 'Asia/Makassar') <= 28 THEN proof.earned_at
           ELSE (date_trunc('month', proof.earned_at AT TIME ZONE 'Asia/Makassar') + interval '1 month')
                AT TIME ZONE 'Asia/Makassar' END),
       CASE WHEN old.id IS NOT NULL THEN old.bonus_rate_id WHEN NOT employee.ineligible THEN rate.id END,
       CASE WHEN old.id IS NOT NULL THEN old.amount_idr
            WHEN employee.ineligible THEN NULL
            WHEN rate.forfeit_below_idr IS NULL OR paid.below IS FALSE THEN rate.amount_idr END,
       old.id, now(), CASE WHEN old.id IS NULL THEN employee.member_id END,
       old.id IS NULL AND employee.ineligible,
       old.id IS NULL AND NOT employee.ineligible AND COALESCE(paid.below, FALSE)
FROM proof
LEFT JOIN LATERAL (
    SELECT id, employee_id, awarded_at, bonus_rate_id, amount_idr FROM hr_bonus_ledger
     WHERE practice_id = proof.practice_id ORDER BY id LIMIT 1
) old ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COUNT(*) = 1 AND bool_and(m.eligible) THEN MIN(m.employee_id) END AS id,
           CASE WHEN COUNT(DISTINCT m.member_id) = 1 THEN MIN(m.member_id) END AS member_id,
           COALESCE(bool_and(m.eligible IS FALSE), FALSE) AS ineligible
    FROM (
        SELECT tm.id AS member_id, e.id AS employee_id,
               (e.is_active = TRUE AND ((e.profile -> 'on_payroll') IS DISTINCT FROM 'false'::jsonb) AND (
                   (tm.active = true OR lower(trim(COALESCE(tm.role, ''))) IN ('group_staff'))
                   AND lower(trim(COALESCE(tm.role, ''))) NOT IN ('client', 'monitoring', 'partner')
                   AND (e.id IS NOT NULL OR (lower(tm.email) LIKE '%@balizero.com'
                        AND lower(trim(COALESCE(tm.role, ''))) NOT IN ('', 'member')))
               ) AND lower(trim(COALESCE(tm.role, ''))) NOT IN ('group_staff')) AS eligible
        FROM team_members tm LEFT JOIN hr_employees e ON e.team_member_id = tm.id
        WHERE lower(trim(tm.email)) = NULLIF(lower(trim(proof.assigned_to)), '')
    ) m
) employee ON TRUE
LEFT JOIN hr_bonus_rates rate ON rate.practice_type_code = proof.code AND rate.is_active
    AND rate.effective_from <= (proof.earned_at AT TIME ZONE 'Asia/Makassar')::date
    AND (rate.effective_to IS NULL OR rate.effective_to >= (proof.earned_at AT TIME ZONE 'Asia/Makassar')::date)
LEFT JOIN LATERAL (SELECT hr_practice_paid_below(proof.practice_id, rate.forfeit_below_idr) AS below) paid ON TRUE
ON CONFLICT (practice_id) DO NOTHING;

-- === ROLLBACK ===
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE amount_idr = 0) THEN
        RAISE EXCEPTION 'Preserve no-bonus receipts before rolling back this migration';
    END IF;
END $$;
DELETE FROM hr_bonus_rates WHERE practice_type_code = 'other_custom' AND amount_idr = 0;
