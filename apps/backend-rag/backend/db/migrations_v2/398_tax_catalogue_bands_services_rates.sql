-- Migration 398: the tax catalogue gets nine monthly bands, the new tax services and their bonus rates.
--
-- Owner approval: 2026-11-27 ("Ok D, come proposto"). Rates are effective from 2026-11-01, as in 388.
--
-- 1. Monthly bands. The bonus uses nine bands per family (0-50 ... 400 or more transactions); Kita had four.
--    The two existing codes tax_monthly_{basic,bundled}_{0_50,50_100} stay as they are. Seven new bands per
--    family are added; each carries the category, price and duration of the list band that contains it
--    (100-150 and 150-200 = the old 100-200 row; 200-250 and above = the old 200-plus row), COPIED from
--    that row so the client price is whatever is stored today (NULL included). The public price list keeps
--    its four bands.
-- 2. The wide codes tax_monthly_{basic,bundled}_{100_200,200_plus} become inactive: no new practice, no
--    rate. They stay for the practices that already use them; nothing migrates those practices.
-- 3. tax_npwp_personal_coretax is renamed "(with KITAS)" (price unchanged) and ten new services are added.
-- 4. 38 bonus rates are upserted (18 bands + 10 existing services + 10 new services). A rate of 0 means
--    "no bonus for this service" (390). tax_registration deliberately gets no rate.
--
-- No practice, receipt or ledger row is touched. Numbered 398: production's last applied migration is 397.
-- Idempotent: ON CONFLICT upserts; a re-run never rewrites a base_price that staff changed since.

-- 1. New monthly bands.
INSERT INTO practice_types (code, name, description, category, base_price, typical_duration_days, is_active)
SELECT v.code, v.name, v.description, src.category, src.base_price, src.typical_duration_days, true
FROM (VALUES
    ('tax_monthly_basic_100_150', 'Monthly Tax Report — 100 to 150 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 100 to 150 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_100_200'),
    ('tax_monthly_basic_150_200', 'Monthly Tax Report — 150 to 200 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 150 to 200 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_100_200'),
    ('tax_monthly_basic_200_250', 'Monthly Tax Report — 200 to 250 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 200 to 250 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_200_plus'),
    ('tax_monthly_basic_250_300', 'Monthly Tax Report — 250 to 300 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 250 to 300 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_200_plus'),
    ('tax_monthly_basic_300_350', 'Monthly Tax Report — 300 to 350 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 300 to 350 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_200_plus'),
    ('tax_monthly_basic_350_400', 'Monthly Tax Report — 350 to 400 transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 350 to 400 transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_200_plus'),
    ('tax_monthly_basic_400_plus', 'Monthly Tax Report — 400 or more transactions (without LKPM & Annual)',
     'Monthly bookkeeping and tax filing for companies with 400 or more transactions. Bank + Cash transactions combined. LKPM and Annual Tax NOT included.',
     'tax_monthly_basic_200_plus'),
    ('tax_monthly_bundled_100_150', 'Monthly Tax Report — 100 to 150 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 100 to 150 transactions.',
     'tax_monthly_bundled_100_200'),
    ('tax_monthly_bundled_150_200', 'Monthly Tax Report — 150 to 200 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 150 to 200 transactions.',
     'tax_monthly_bundled_100_200'),
    ('tax_monthly_bundled_200_250', 'Monthly Tax Report — 200 to 250 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 200 to 250 transactions.',
     'tax_monthly_bundled_200_plus'),
    ('tax_monthly_bundled_250_300', 'Monthly Tax Report — 250 to 300 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 250 to 300 transactions.',
     'tax_monthly_bundled_200_plus'),
    ('tax_monthly_bundled_300_350', 'Monthly Tax Report — 300 to 350 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 300 to 350 transactions.',
     'tax_monthly_bundled_200_plus'),
    ('tax_monthly_bundled_350_400', 'Monthly Tax Report — 350 to 400 transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 350 to 400 transactions.',
     'tax_monthly_bundled_200_plus'),
    ('tax_monthly_bundled_400_plus', 'Monthly Tax Report — 400 or more transactions (incl. LKPM & Annual)',
     'Bundled monthly bookkeeping, LKPM filing and Annual Tax report for companies with 400 or more transactions.',
     'tax_monthly_bundled_200_plus')
) AS v(code, name, description, source_code)
JOIN practice_types src ON src.code = v.source_code
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    is_active = true,
    updated_at = CURRENT_TIMESTAMP;

-- 2. The four wide codes stop taking new practices.
UPDATE practice_types
SET is_active = false,
    updated_at = CURRENT_TIMESTAMP
WHERE code IN (
    'tax_monthly_basic_100_200',
    'tax_monthly_basic_200_plus',
    'tax_monthly_bundled_100_200',
    'tax_monthly_bundled_200_plus'
);

-- 3. The existing NPWP service is the "with KITAS" one; the new services.
UPDATE practice_types
SET name = 'NPWP Personal + Coretax Activation (with KITAS)',
    description = 'Personal Indonesian tax ID (NPWP) issuance with activation on the Coretax platform, for a person with a KITAS. Per person.',
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'tax_npwp_personal_coretax';

INSERT INTO practice_types (code, name, description, category, base_price, typical_duration_days, is_active)
VALUES
    ('tax_npwp_personal_no_kitas', 'NPWP Personal + Coretax Activation (without KITAS)',
     'Personal Indonesian tax ID (NPWP) issuance with activation on the Coretax platform, for a person without a KITAS. Per person.',
     'tax', 1500000, NULL, true),
    ('tax_sertel', 'Electronic Certificate (SERTEL)',
     'Issuance of the electronic certificate (SERTEL) used to sign tax filings on Coretax.',
     'tax', 1000000, NULL, true),
    ('tax_coretax_activation_personal', 'Coretax Activation — Personal',
     'Stand-alone Coretax activation for an existing personal NPWP.',
     'tax', 1000000, NULL, true),
    ('tax_coretax_activation_company', 'Coretax Activation — Company',
     'Stand-alone Coretax activation for an existing company NPWP.',
     'tax', 1000000, NULL, true),
    ('tax_kpp_accompaniment', 'Tax Office (KPP) Accompaniment',
     'Accompaniment of the client to the tax office (KPP) for a visit or a filing at the counter.',
     'tax', 1000000, NULL, true),
    ('tax_abt_registration', 'Groundwater Tax (ABT) Registration',
     'Registration of a site for the groundwater tax (ABT) with the regional tax authority.',
     'tax', 2500000, NULL, true),
    ('tax_meeting', 'Tax Meeting',
     'Tax meeting with a tax consultant, with a written summary.',
     'tax', 1000000, NULL, true),
    ('tax_monthly_billing_bpjs_tk', 'Monthly Billing — BPJS Ketenagakerjaan',
     'Monthly BPJS Ketenagakerjaan billing and payment. Included in the monthly tax report price.',
     'tax', NULL, NULL, true),
    ('tax_monthly_billing_bpjs_ks', 'Monthly Billing — BPJS Kesehatan',
     'Monthly BPJS Kesehatan billing and payment. Included in the monthly tax report price.',
     'tax', NULL, NULL, true),
    ('tax_monthly_billing_npwpd', 'Monthly Billing — NPWPD',
     'Monthly regional tax (NPWPD) billing and payment. Included in the monthly tax report price.',
     'tax', NULL, NULL, true)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    typical_duration_days = EXCLUDED.typical_duration_days,
    is_active = true,
    updated_at = CURRENT_TIMESTAMP;

-- 4. Bonus rates.
INSERT INTO hr_bonus_rates (practice_type_code, amount_idr, effective_from, is_active, notes)
VALUES
    ('tax_monthly_basic_0_50', 150000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_50_100', 250000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_100_150', 350000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_150_200', 450000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_200_250', 550000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_250_300', 650000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_300_350', 750000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_350_400', 850000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_basic_400_plus', 950000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_0_50', 150000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_50_100', 250000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_100_150', 350000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_150_200', 450000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_200_250', 550000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_250_300', 650000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_300_350', 750000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_350_400', 850000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_bundled_400_plus', 950000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_personal', 150000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_personal_addl', 300000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_company', 1000000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_reporting', 500000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_pkg_a', 1000000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_pkg_b', 1000000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_pkg_c', 1000000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_annual_pkg_d', 1000000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_lkpm_yearly', 100000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_npwp_personal_coretax', 125000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_npwp_personal_no_kitas', 100000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_sertel', 100000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_coretax_activation_personal', 100000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_coretax_activation_company', 100000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_kpp_accompaniment', 150000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_abt_registration', 500000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_meeting', 200000, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_billing_bpjs_tk', 0, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_billing_bpjs_ks', 0, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue'),
    ('tax_monthly_billing_npwpd', 0, '2026-11-01', TRUE, 'Bonus reform 2026-11-27: tax catalogue')
ON CONFLICT (practice_type_code) DO UPDATE SET
    amount_idr = EXCLUDED.amount_idr, forfeit_below_idr = NULL, effective_from = EXCLUDED.effective_from,
    is_active = TRUE, effective_to = NULL, notes = EXCLUDED.notes, updated_at = now();

-- Postcondition: refuse to record this migration as applied unless the catalogue reads as ruled.
DO $$
DECLARE
    expected record;
    got numeric;
    n integer;
BEGIN
    SELECT count(*) INTO n FROM practice_types WHERE is_active AND code = ANY(ARRAY[
        'tax_monthly_basic_0_50',
        'tax_monthly_basic_50_100',
        'tax_monthly_basic_100_150',
        'tax_monthly_basic_150_200',
        'tax_monthly_basic_200_250',
        'tax_monthly_basic_250_300',
        'tax_monthly_basic_300_350',
        'tax_monthly_basic_350_400',
        'tax_monthly_basic_400_plus',
        'tax_monthly_bundled_0_50',
        'tax_monthly_bundled_50_100',
        'tax_monthly_bundled_100_150',
        'tax_monthly_bundled_150_200',
        'tax_monthly_bundled_200_250',
        'tax_monthly_bundled_250_300',
        'tax_monthly_bundled_300_350',
        'tax_monthly_bundled_350_400',
        'tax_monthly_bundled_400_plus']);
    IF n <> 18 THEN RAISE EXCEPTION 'migration 398: % active monthly bands, expected 18', n; END IF;
    SELECT count(*) INTO n FROM practice_types
     WHERE NOT is_active AND code = ANY(ARRAY['tax_monthly_basic_100_200', 'tax_monthly_basic_200_plus',
                    'tax_monthly_bundled_100_200', 'tax_monthly_bundled_200_plus']);
    IF n <> 4 THEN RAISE EXCEPTION 'migration 398: % of the 4 wide codes are inactive, expected 4', n; END IF;
    FOR expected IN
        SELECT * FROM (VALUES
            ('tax_monthly_basic_0_50', 150000::bigint),
            ('tax_monthly_basic_50_100', 250000::bigint),
            ('tax_monthly_basic_100_150', 350000::bigint),
            ('tax_monthly_basic_150_200', 450000::bigint),
            ('tax_monthly_basic_200_250', 550000::bigint),
            ('tax_monthly_basic_250_300', 650000::bigint),
            ('tax_monthly_basic_300_350', 750000::bigint),
            ('tax_monthly_basic_350_400', 850000::bigint),
            ('tax_monthly_basic_400_plus', 950000::bigint),
            ('tax_monthly_bundled_0_50', 150000::bigint),
            ('tax_monthly_bundled_50_100', 250000::bigint),
            ('tax_monthly_bundled_100_150', 350000::bigint),
            ('tax_monthly_bundled_150_200', 450000::bigint),
            ('tax_monthly_bundled_200_250', 550000::bigint),
            ('tax_monthly_bundled_250_300', 650000::bigint),
            ('tax_monthly_bundled_300_350', 750000::bigint),
            ('tax_monthly_bundled_350_400', 850000::bigint),
            ('tax_monthly_bundled_400_plus', 950000::bigint),
            ('tax_annual_personal', 150000::bigint),
            ('tax_annual_personal_addl', 300000::bigint),
            ('tax_annual_company', 1000000::bigint),
            ('tax_annual_reporting', 500000::bigint),
            ('tax_annual_pkg_a', 1000000::bigint),
            ('tax_annual_pkg_b', 1000000::bigint),
            ('tax_annual_pkg_c', 1000000::bigint),
            ('tax_annual_pkg_d', 1000000::bigint),
            ('tax_lkpm_yearly', 100000::bigint),
            ('tax_npwp_personal_coretax', 125000::bigint),
            ('tax_npwp_personal_no_kitas', 100000::bigint),
            ('tax_sertel', 100000::bigint),
            ('tax_coretax_activation_personal', 100000::bigint),
            ('tax_coretax_activation_company', 100000::bigint),
            ('tax_kpp_accompaniment', 150000::bigint),
            ('tax_abt_registration', 500000::bigint),
            ('tax_meeting', 200000::bigint),
            ('tax_monthly_billing_bpjs_tk', 0::bigint),
            ('tax_monthly_billing_bpjs_ks', 0::bigint),
            ('tax_monthly_billing_npwpd', 0::bigint)
        ) AS t(code, amount)
    LOOP
        SELECT amount_idr INTO got FROM hr_bonus_rates WHERE practice_type_code = expected.code AND is_active;
        IF got IS DISTINCT FROM expected.amount THEN
            RAISE EXCEPTION 'migration 398: rate of % is %, expected %', expected.code, got, expected.amount;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM hr_bonus_rates WHERE practice_type_code = 'tax_registration') THEN
        RAISE EXCEPTION 'migration 398: tax_registration must carry no rate';
    END IF;
    IF EXISTS (SELECT 1 FROM hr_bonus_rates WHERE practice_type_code IN (
            'tax_monthly_basic_100_200', 'tax_monthly_basic_200_plus',
            'tax_monthly_bundled_100_200', 'tax_monthly_bundled_200_plus')) THEN
        RAISE EXCEPTION 'migration 398: a wide monthly code must carry no rate';
    END IF;
END;
$$;

-- === ROLLBACK ===
-- Soft-disable, never a hard removal: practices reference practice_types by foreign key. Refuses once a
-- receipt exists for one of the codes this migration priced: roll forward instead.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM hr_delivery_bonus_events WHERE service_code IN (
    'tax_monthly_basic_0_50',
    'tax_monthly_basic_50_100',
    'tax_monthly_basic_100_150',
    'tax_monthly_basic_150_200',
    'tax_monthly_basic_200_250',
    'tax_monthly_basic_250_300',
    'tax_monthly_basic_300_350',
    'tax_monthly_basic_350_400',
    'tax_monthly_basic_400_plus',
    'tax_monthly_bundled_0_50',
    'tax_monthly_bundled_50_100',
    'tax_monthly_bundled_100_150',
    'tax_monthly_bundled_150_200',
    'tax_monthly_bundled_200_250',
    'tax_monthly_bundled_250_300',
    'tax_monthly_bundled_300_350',
    'tax_monthly_bundled_350_400',
    'tax_monthly_bundled_400_plus',
    'tax_annual_personal',
    'tax_annual_personal_addl',
    'tax_annual_company',
    'tax_annual_reporting',
    'tax_annual_pkg_a',
    'tax_annual_pkg_b',
    'tax_annual_pkg_c',
    'tax_annual_pkg_d',
    'tax_lkpm_yearly',
    'tax_npwp_personal_coretax',
    'tax_npwp_personal_no_kitas',
    'tax_sertel',
    'tax_coretax_activation_personal',
    'tax_coretax_activation_company',
    'tax_kpp_accompaniment',
    'tax_abt_registration',
    'tax_meeting',
    'tax_monthly_billing_bpjs_tk',
    'tax_monthly_billing_bpjs_ks',
    'tax_monthly_billing_npwpd')) THEN
        RAISE EXCEPTION 'Preserve the receipts of the tax catalogue before rolling back this migration';
    END IF;
END $$;
DELETE FROM hr_bonus_rates WHERE practice_type_code IN (
    'tax_monthly_basic_0_50',
    'tax_monthly_basic_50_100',
    'tax_monthly_basic_100_150',
    'tax_monthly_basic_150_200',
    'tax_monthly_basic_200_250',
    'tax_monthly_basic_250_300',
    'tax_monthly_basic_300_350',
    'tax_monthly_basic_350_400',
    'tax_monthly_basic_400_plus',
    'tax_monthly_bundled_0_50',
    'tax_monthly_bundled_50_100',
    'tax_monthly_bundled_100_150',
    'tax_monthly_bundled_150_200',
    'tax_monthly_bundled_200_250',
    'tax_monthly_bundled_250_300',
    'tax_monthly_bundled_300_350',
    'tax_monthly_bundled_350_400',
    'tax_monthly_bundled_400_plus',
    'tax_annual_personal',
    'tax_annual_personal_addl',
    'tax_annual_company',
    'tax_annual_reporting',
    'tax_annual_pkg_a',
    'tax_annual_pkg_b',
    'tax_annual_pkg_c',
    'tax_annual_pkg_d',
    'tax_lkpm_yearly',
    'tax_npwp_personal_coretax',
    'tax_npwp_personal_no_kitas',
    'tax_sertel',
    'tax_coretax_activation_personal',
    'tax_coretax_activation_company',
    'tax_kpp_accompaniment',
    'tax_abt_registration',
    'tax_meeting',
    'tax_monthly_billing_bpjs_tk',
    'tax_monthly_billing_bpjs_ks',
    'tax_monthly_billing_npwpd'
);
UPDATE practice_types SET is_active = false, updated_at = CURRENT_TIMESTAMP WHERE code IN (
    'tax_monthly_basic_100_150',
    'tax_monthly_basic_150_200',
    'tax_monthly_basic_200_250',
    'tax_monthly_basic_250_300',
    'tax_monthly_basic_300_350',
    'tax_monthly_basic_350_400',
    'tax_monthly_basic_400_plus',
    'tax_monthly_bundled_100_150',
    'tax_monthly_bundled_150_200',
    'tax_monthly_bundled_200_250',
    'tax_monthly_bundled_250_300',
    'tax_monthly_bundled_300_350',
    'tax_monthly_bundled_350_400',
    'tax_monthly_bundled_400_plus',
    'tax_npwp_personal_no_kitas',
    'tax_sertel',
    'tax_coretax_activation_personal',
    'tax_coretax_activation_company',
    'tax_kpp_accompaniment',
    'tax_abt_registration',
    'tax_meeting',
    'tax_monthly_billing_bpjs_tk',
    'tax_monthly_billing_bpjs_ks',
    'tax_monthly_billing_npwpd'
);
UPDATE practice_types SET is_active = true, updated_at = CURRENT_TIMESTAMP WHERE code IN (
    'tax_monthly_basic_100_200',
    'tax_monthly_basic_200_plus',
    'tax_monthly_bundled_100_200',
    'tax_monthly_bundled_200_plus'
);
UPDATE practice_types
SET name = 'NPWP Personal + Coretax Activation',
    description = 'Personal Indonesian tax ID (NPWP) issuance with activation on the Coretax platform. Per person.',
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'tax_npwp_personal_coretax';
