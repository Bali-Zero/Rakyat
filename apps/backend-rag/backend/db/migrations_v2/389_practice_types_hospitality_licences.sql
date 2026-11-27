-- Migration 389: add hospitality & F&B operational licences to practice_types
--
-- Ruled by the owner on 2026-11-27:
--   * SLHS (Sertifikat Laik Higiene Sanitasi) — 10.000.000 IDR
--   * SLS — Villa & Boutique Accommodation    —  6.000.000 IDR
--   * SLS — Hotel & Resort                    — 12.000.000 IDR
--   * Alcohol License — Golongan A            —  4.000.000 IDR
--   * Alcohol License — Golongan B + C        — 10.000.000 IDR
--   * Alcohol License — Golongan A + B + C    — 12.000.000 IDR
--
-- All six rows sit in category 'company' (invoiced by PT Bali Nol Impresariat),
-- match the exact service names in bali_zero_official_prices_2026.json for
-- test_price_store_reconciliation.py, and carry curated required_documents
-- checklists that crm_practices.py seeds into practice_required_documents.
--
-- Numbered 389: main is at 387 and open PR #992 carries 388.
--
-- Idempotent: ON CONFLICT (code) DO UPDATE.

INSERT INTO practice_types (
    code, name, description, category, base_price,
    typical_duration_days, required_documents, is_active
)
VALUES
(
    'company_slhs',
    'SLHS (Sertifikat Laik Higiene Sanitasi)',
    'Food hygiene and sanitation certificate (SLHS) from Dinas Kesehatan for restaurants, cafes and food and beverage businesses.',
    'company',
    10000000,
    28,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP & NIB (KBLI 56xxx)',
        'Director KTP or Passport & KITAS',
        'Premises Lease Agreement & PBG/SLF',
        'Kitchen Layout, Photos & Geotagging',
        'PKP Food Safety Training Certificate',
        'KAN Lab Test Results (Water, Food, Swabs)',
        'Food Handler Medical Checkups & SOPs'
    ],
    true
),
(
    'company_sls_villa',
    'SLS — Villa & Boutique Accommodation',
    'Healthworthiness certificate (Sertifikat Laik Sehat) for standalone villas and boutique stays without a commercial restaurant.',
    'company',
    6000000,
    21,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP & NIB (Accommodation KBLI)',
        'Director KTP or Passport & KITAS',
        'Villa Lease/Title Deed & PBG/SLF',
        'Clean Water & Pool KAN Lab Results',
        'Sanitation, Pest Control & Waste SOPs'
    ],
    true
),
(
    'company_sls_hotel',
    'SLS — Hotel & Resort',
    'Healthworthiness certificate (Sertifikat Laik Sehat) for hotels, resorts and accommodations operating a commercial kitchen or restaurant.',
    'company',
    12000000,
    35,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP & NIB (Hotel & F&B KBLI)',
        'Director KTP or Passport & KITAS',
        'Property Lease/Deed, PKKPR & PBG/SLF',
        'Site Plan, Room Count & Geotagged Photos',
        'Water, Pool, Food & Air KAN Lab Results',
        'Staff Health Certificates & Hygiene Training',
        'Environmental (SPPL/UKL-UPL) & Waste SOPs'
    ],
    true
),
(
    'company_alcohol_a',
    'Alcohol License — Golongan A',
    'Direct-sales alcohol licence (SKPL-A & NPPBKC) for Golongan A beverages up to 5% ABV.',
    'company',
    4000000,
    42,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP, NIB & Verified Sertifikat Standar',
        'Director KTP or Passport & KITAS',
        'Premises Lease, Floor Plan & Geotagged Photos',
        'Zoning Clearance (>100m from Schools/Worship)',
        'Official Distributor Appointment Letter (Gol. A)',
        'NPWPD Local Tax ID & SLHS/SLS Certificate'
    ],
    true
),
(
    'company_alcohol_bc',
    'Alcohol License — Golongan B + C',
    'Direct-sales alcohol licence (SKPL-B/C & NPPBKC) for Golongan B (>5% to 20% ABV) and Golongan C (>20% to 55% ABV) beverages.',
    'company',
    10000000,
    49,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP, NIB & Verified Sertifikat Standar',
        'Director KTP or Passport & KITAS',
        'Premises Lease, Bar/Storage Plan & Photos',
        'Zoning Clearance (>100m from Schools/Worship)',
        'Authorized Sub-Distributor Agreement (Gol. B & C)',
        'Bea Cukai NPPBKC Excise Application Dossier',
        'NPWPD Local Tax ID & SLHS/SLS Certificate'
    ],
    true
),
(
    'company_alcohol_abc',
    'Alcohol License — Golongan A + B + C',
    'Full direct-sales alcohol licence (SKPL-A/B/C & NPPBKC) covering all three classes: Golongan A, B and C.',
    'company',
    12000000,
    49,
    ARRAY[
        'Akta Pendirian & SK Kemenkumham',
        'Company NPWP, NIB & Verified Sertifikat Standar',
        'Director KTP or Passport & KITAS',
        'Premises Lease, Bar/Storage Plan & Photos',
        'Zoning Clearance (>100m from Schools/Worship)',
        'Official Distributor Agreements (Gol. A, B & C)',
        'Bea Cukai NPPBKC Excise Application Dossier',
        'Excise Stamp & Segregated Storage SOP',
        'NPWPD Local Tax ID & SLHS/SLS Certificate'
    ],
    true
)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    base_price = EXCLUDED.base_price,
    typical_duration_days = EXCLUDED.typical_duration_days,
    required_documents = EXCLUDED.required_documents,
    is_active = true,
    updated_at = CURRENT_TIMESTAMP;

-- Postcondition: refuse to record this migration as applied unless all six rows
-- exist, are active in 'company', carry their ruled prices and non-empty checklists.
DO $$
DECLARE
    expected record;
    row_active boolean;
    row_price numeric;
    row_category text;
    doc_count integer;
BEGIN
    FOR expected IN
        SELECT * FROM (VALUES
            ('company_slhs',        10000000::numeric, 8),
            ('company_sls_villa',    6000000::numeric, 6),
            ('company_sls_hotel',   12000000::numeric, 8),
            ('company_alcohol_a',    4000000::numeric, 7),
            ('company_alcohol_bc',  10000000::numeric, 8),
            ('company_alcohol_abc', 12000000::numeric, 9)
        ) AS t(code, price, docs)
    LOOP
        SELECT is_active, base_price, category, coalesce(cardinality(required_documents), 0)
          INTO row_active, row_price, row_category, doc_count
          FROM practice_types
         WHERE code = expected.code;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'migration 389: practice_types.% does not exist', expected.code;
        END IF;

        IF row_active IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'migration 389: practice_types.% is not active', expected.code;
        END IF;

        IF row_price IS DISTINCT FROM expected.price THEN
            RAISE EXCEPTION
                'migration 389: practice_types.% base_price is %, expected %',
                expected.code, row_price, expected.price;
        END IF;

        IF row_category IS DISTINCT FROM 'company' THEN
            RAISE EXCEPTION
                'migration 389: practice_types.% category is %, expected company',
                expected.code, row_category;
        END IF;

        IF doc_count IS DISTINCT FROM expected.docs THEN
            RAISE EXCEPTION
                'migration 389: practice_types.% required_documents count is %, expected %',
                expected.code, doc_count, expected.docs;
        END IF;
    END LOOP;
END;
$$;

-- === ROLLBACK ===

-- Soft-disable, never a hard removal: practices reference practice_types by
-- foreign key, so rows already in use cannot be deleted.
UPDATE practice_types
SET is_active = false,
    updated_at = CURRENT_TIMESTAMP
WHERE code IN (
    'company_slhs',
    'company_sls_villa',
    'company_sls_hotel',
    'company_alcohol_a',
    'company_alcohol_bc',
    'company_alcohol_abc'
);
