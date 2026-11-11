-- 352_client_expiry_alerts_view_finite_dates.sql
-- depends: 351
-- A single non-finite date in one of the four source columns made every
-- unfiltered read of client_expiry_alerts_view fail with "cannot subtract
-- infinite dates" (days_until_expiry = <date> - CURRENT_DATE), taking down the
-- team expiry-alert endpoints, the CRM agent deadlines tool and the profile
-- alerts. asyncpg encodes Python date.min (0001-01-01) as -infinity and
-- date.max (9999-12-31) as infinity, so an unparseable/placeholder date from an
-- intake or form path lands as a non-finite date.
--
-- Decision: a non-finite date is EXCLUDED from the view, exactly like a NULL
-- date already is (WHERE ... IS NOT NULL). It is "no usable expiry date", and
-- every consumer either filters on alert_color/expiry_date or counts by colour;
-- a kept row would surface as a false 'expired' alert with a year-1 date.
-- Column list, names, types and order are unchanged (CREATE OR REPLACE VIEW).
-- The source definition is apply_migration_033.py plus the isfinite() guards.
--
-- The guard is in two places on purpose. A caller's filter on days_until_expiry
-- is pushed down into the same scan filter as the WHERE guard, and Postgres
-- does not promise which of the two it evaluates first, so the subtraction is
-- wrapped in CASE WHEN isfinite(...), the construct that does order evaluation.
-- Data rows are not touched: a human supplies the real date.

CREATE OR REPLACE VIEW client_expiry_alerts_view AS
SELECT
    'client' as entity_type,
    c.id as entity_id,
    c.full_name as entity_name,
    c.id as client_id,
    c.full_name as client_name,
    'passport' as document_type,
    c.passport_expiry as expiry_date,
    CASE WHEN isfinite(c.passport_expiry) THEN c.passport_expiry - CURRENT_DATE END as days_until_expiry,
    CASE
        WHEN c.passport_expiry <= CURRENT_DATE THEN 'expired'
        WHEN c.passport_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN c.passport_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM clients c
WHERE c.passport_expiry IS NOT NULL
  AND isfinite(c.passport_expiry)
  AND c.status = 'active'

UNION ALL

SELECT
    'family_member' as entity_type,
    fm.id as entity_id,
    fm.full_name as entity_name,
    fm.client_id,
    c.full_name as client_name,
    'passport' as document_type,
    fm.passport_expiry as expiry_date,
    CASE WHEN isfinite(fm.passport_expiry) THEN fm.passport_expiry - CURRENT_DATE END as days_until_expiry,
    CASE
        WHEN fm.passport_expiry <= CURRENT_DATE THEN 'expired'
        WHEN fm.passport_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN fm.passport_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM client_family_members fm
JOIN clients c ON fm.client_id = c.id
WHERE fm.passport_expiry IS NOT NULL
  AND isfinite(fm.passport_expiry)

UNION ALL

SELECT
    'family_member' as entity_type,
    fm.id as entity_id,
    fm.full_name as entity_name,
    fm.client_id,
    c.full_name as client_name,
    'visa' as document_type,
    fm.visa_expiry as expiry_date,
    CASE WHEN isfinite(fm.visa_expiry) THEN fm.visa_expiry - CURRENT_DATE END as days_until_expiry,
    CASE
        WHEN fm.visa_expiry <= CURRENT_DATE THEN 'expired'
        WHEN fm.visa_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN fm.visa_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM client_family_members fm
JOIN clients c ON fm.client_id = c.id
WHERE fm.visa_expiry IS NOT NULL
  AND isfinite(fm.visa_expiry)

UNION ALL

SELECT
    'document' as entity_type,
    d.id as entity_id,
    d.document_type as entity_name,
    d.client_id,
    c.full_name as client_name,
    d.document_type,
    d.expiry_date,
    CASE WHEN isfinite(d.expiry_date) THEN d.expiry_date - CURRENT_DATE END as days_until_expiry,
    CASE
        WHEN d.expiry_date <= CURRENT_DATE THEN 'expired'
        WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM documents d
JOIN clients c ON d.client_id = c.id
WHERE d.expiry_date IS NOT NULL
  AND isfinite(d.expiry_date)
  AND d.status != 'rejected'
  AND (d.is_archived IS NULL OR d.is_archived = false);

-- === ROLLBACK ===
-- Restores the pre-352 definition (re-opens the single-row failure mode).
CREATE OR REPLACE VIEW client_expiry_alerts_view AS
SELECT
    'client' as entity_type,
    c.id as entity_id,
    c.full_name as entity_name,
    c.id as client_id,
    c.full_name as client_name,
    'passport' as document_type,
    c.passport_expiry as expiry_date,
    c.passport_expiry - CURRENT_DATE as days_until_expiry,
    CASE
        WHEN c.passport_expiry <= CURRENT_DATE THEN 'expired'
        WHEN c.passport_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN c.passport_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM clients c
WHERE c.passport_expiry IS NOT NULL
  AND c.status = 'active'

UNION ALL

SELECT
    'family_member' as entity_type,
    fm.id as entity_id,
    fm.full_name as entity_name,
    fm.client_id,
    c.full_name as client_name,
    'passport' as document_type,
    fm.passport_expiry as expiry_date,
    fm.passport_expiry - CURRENT_DATE as days_until_expiry,
    CASE
        WHEN fm.passport_expiry <= CURRENT_DATE THEN 'expired'
        WHEN fm.passport_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN fm.passport_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM client_family_members fm
JOIN clients c ON fm.client_id = c.id
WHERE fm.passport_expiry IS NOT NULL

UNION ALL

SELECT
    'family_member' as entity_type,
    fm.id as entity_id,
    fm.full_name as entity_name,
    fm.client_id,
    c.full_name as client_name,
    'visa' as document_type,
    fm.visa_expiry as expiry_date,
    fm.visa_expiry - CURRENT_DATE as days_until_expiry,
    CASE
        WHEN fm.visa_expiry <= CURRENT_DATE THEN 'expired'
        WHEN fm.visa_expiry <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN fm.visa_expiry <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM client_family_members fm
JOIN clients c ON fm.client_id = c.id
WHERE fm.visa_expiry IS NOT NULL

UNION ALL

SELECT
    'document' as entity_type,
    d.id as entity_id,
    d.document_type as entity_name,
    d.client_id,
    c.full_name as client_name,
    d.document_type,
    d.expiry_date,
    d.expiry_date - CURRENT_DATE as days_until_expiry,
    CASE
        WHEN d.expiry_date <= CURRENT_DATE THEN 'expired'
        WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
        WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
        ELSE 'green'
    END as alert_color,
    c.assigned_to
FROM documents d
JOIN clients c ON d.client_id = c.id
WHERE d.expiry_date IS NOT NULL
  AND d.status != 'rejected'
  AND (d.is_archived IS NULL OR d.is_archived = false);
