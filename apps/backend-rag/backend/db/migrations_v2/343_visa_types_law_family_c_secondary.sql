-- Migration 343: regulation-sourced secondary C indices (C7, C8, C22) only.
-- Hand-assembled subset: importer has no subset input independent of family_c.json.
-- Column order, literals, case guard and rollback mirror importer.LAW_COLUMNS.
-- The complete cards are retained; activity-to-index mapping remains TO_VERIFY.
-- No code-specific page hash, stay, sponsor decision or client price is invented.
-- Conflict updates write Law columns only; all legacy columns are preserved.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Guard: refuse to run if a row already holds one of these codes in another case
-- (the UNIQUE on code is case-sensitive, so the upsert below would add a duplicate).
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM visa_types WHERE code <> upper(code) AND upper(code) IN ('C22', 'C7', 'C8')) THEN
        RAISE EXCEPTION 'visa_types holds a non-uppercase duplicate of a family code; fix the case first';
    END IF;
END
$guard$;

INSERT INTO visa_types (code, name, category, family, official_name_id, permit_class, is_sold, price_keys, entry_type, sponsor, apply_channel, initial_stay_days, extendable, max_total_days, max_total_months, itap, law_card, source_url, page_sha256, retrieved_at, card_sha256, verification_status, valid_from, valid_until, superseded_by)
VALUES (
    'C22',
    'C22 Visa Kunjungan Pemagangan',
    'Visit Visa',
    'C',
    'C22 Visa Kunjungan Pemagangan',
    'VISIT_SINGLE',
    NULL,
    $j$[]$j$::jsonb,
    'SINGLE',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    $j${"allowed_activities": "see definition_verbatim", "code": "C22", "definition_verbatim": "pemagangan.", "entry": "single", "extendable": null, "jenis_visa_verbatim": "Visa kunjungan 1 (satu) kali perjalanan dapat diberikan kepada Orang Asing untuk melakukan kegiatan:", "ketentuan_lain_verbatim": null, "notes": ["The official index lists this code; the regulations name activities, not these alphanumeric indices. The title comes from the official index, not from the regulation. Code-to-activity mapping and any statutory parent/child relationship remain TO_VERIFY pending the classification decision.", "Only whitespace is normalized in regulatory excerpts. Pasal 19 excerpts are activity-level evidence, not a complete visa-specific checklist. No eligibility, stay, extension, sponsorship exception or price is inherited from a child card.", "page_sha256 is null because no substantive code-specific page was retrieved. PDF content hashes are recorded separately in source; the DB verification_status remains TO_VERIFY."], "official_name_id": "C22 Visa Kunjungan Pemagangan", "page_sha256": null, "pengajuan_intro_verbatim": "Permohonan Visa kunjungan 1 (satu) kali perjalanan bagi Orang Asing diajukan melalui aplikasi dengan melampirkan:", "pnbp_verbatim_for_reference_only": null, "prohibited_or_conditions_verbatim": null, "requirements_verbatim": [{"n": 1, "section": "Pasal 19 ayat (5) huruf t (as replaced by Permenkumham 11/2024 Pasal I angka 6)", "text": "untuk pemagangan, berupa perjanjian pemagangan atau keterangan dari instansi pemerintah atau lembaga swasta selaku penyelenggara kegiatan."}], "retrieved_at": "2026-10-18T18:40:59Z", "source": {"amendment": {"citation": "Permenkumham 11/2024 Pasal I angka 6 (Pasal 19 ayat (1) and ayat (5))", "document_sha256": "ecbbeccc196145f47f24e68473a27c19d6f0408e607abb365ce9acd22cbf5f02", "url": "https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf"}, "citation": "Permenkumham 22/2023 Pasal 13 ayat (1) huruf z", "classification_basis_citation": "Permenkumham 22/2023 Pasal 70 ayat (3)", "classification_basis_verbatim": "Ketentuan mengenai klasifikasi Visa sebagaimana dimaksud pada ayat (1) dan ayat (2) ditetapkan oleh Menteri.", "code_to_activity_status": "TO_VERIFY", "document_sha256": "2e9165804c38e9a60b4f1c4a046bd727a97043d7386be90d608c68db31c015d4", "fidelity": "VERBATIM_REGULATION_WHITESPACE_NORMALIZED", "index_evidence": {"retrieved_at": "2026-10-18T18:40:59Z", "url": "https://kanwilsultra.imigrasi.go.id/wna/daftar-visa-indonesia", "verbatim": "C22 Visa Kunjungan Pemagangan"}, "type": "REGULATION"}, "source_url": "https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf", "sponsor_required": {"as_stated_boolean": null, "invitation_requirement_see": "requirements_verbatim", "verbatim": ["bukti penjaminan dari Penjamin, kecuali untuk kunjungan tertentu;"]}, "status": "REGULATION_SOURCED_TO_VERIFY", "stay_days": null, "to_verify": ["source.code_to_activity_status", "statutory_parent_child_relationship", "page_sha256", "stay_days", "extendable", "where_to_apply", "sponsor_required.as_stated_boolean", "complete_code_specific_requirements", "prohibited_or_conditions_verbatim", "ketentuan_lain_verbatim", "pnbp_verbatim_for_reference_only", "is_sold", "price_keys"], "where_to_apply": null}$j$::jsonb,
    'https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf',
    NULL,  -- pragma: allowlist secret
    '2026-10-18T18:40:59Z'::timestamptz,
    '3613685d584f3d22c4a6d9a852cec41e752a9e48f16c6b19651e7eade597ee0c',  -- pragma: allowlist secret
    'TO_VERIFY',
    NULL,
    NULL,
    NULL
)
ON CONFLICT (code) DO UPDATE SET
    family = EXCLUDED.family,
    official_name_id = EXCLUDED.official_name_id,
    permit_class = EXCLUDED.permit_class,
    is_sold = EXCLUDED.is_sold,
    price_keys = EXCLUDED.price_keys,
    entry_type = EXCLUDED.entry_type,
    sponsor = EXCLUDED.sponsor,
    apply_channel = EXCLUDED.apply_channel,
    initial_stay_days = EXCLUDED.initial_stay_days,
    extendable = EXCLUDED.extendable,
    max_total_days = EXCLUDED.max_total_days,
    max_total_months = EXCLUDED.max_total_months,
    itap = EXCLUDED.itap,
    law_card = EXCLUDED.law_card,
    source_url = EXCLUDED.source_url,
    page_sha256 = EXCLUDED.page_sha256,
    retrieved_at = EXCLUDED.retrieved_at,
    card_sha256 = EXCLUDED.card_sha256,
    verification_status = EXCLUDED.verification_status,
    valid_from = EXCLUDED.valid_from,
    valid_until = EXCLUDED.valid_until,
    superseded_by = EXCLUDED.superseded_by;

INSERT INTO visa_types (code, name, category, family, official_name_id, permit_class, is_sold, price_keys, entry_type, sponsor, apply_channel, initial_stay_days, extendable, max_total_days, max_total_months, itap, law_card, source_url, page_sha256, retrieved_at, card_sha256, verification_status, valid_from, valid_until, superseded_by)
VALUES (
    'C7',
    'C7 Visa Kunjungan Penampilan Seni dan Budaya',
    'Visit Visa',
    'C',
    'C7 Visa Kunjungan Penampilan Seni dan Budaya',
    'VISIT_SINGLE',
    NULL,
    $j$[]$j$::jsonb,
    'SINGLE',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    $j${"allowed_activities": "see definition_verbatim", "code": "C7", "definition_verbatim": "seni dan budaya;", "entry": "single", "extendable": null, "jenis_visa_verbatim": "Visa kunjungan 1 (satu) kali perjalanan dapat diberikan kepada Orang Asing untuk melakukan kegiatan:", "ketentuan_lain_verbatim": null, "notes": ["The official index lists this code; the regulations name activities, not these alphanumeric indices. The title comes from the official index, not from the regulation. Code-to-activity mapping and any statutory parent/child relationship remain TO_VERIFY pending the classification decision.", "Only whitespace is normalized in regulatory excerpts. Pasal 19 excerpts are activity-level evidence, not a complete visa-specific checklist. No eligibility, stay, extension, sponsorship exception or price is inherited from a child card.", "page_sha256 is null because no substantive code-specific page was retrieved. PDF content hashes are recorded separately in source; the DB verification_status remains TO_VERIFY."], "official_name_id": "C7 Visa Kunjungan Penampilan Seni dan Budaya", "page_sha256": null, "pengajuan_intro_verbatim": "Permohonan Visa kunjungan 1 (satu) kali perjalanan bagi Orang Asing diajukan melalui aplikasi dengan melampirkan:", "pnbp_verbatim_for_reference_only": null, "prohibited_or_conditions_verbatim": null, "requirements_verbatim": [{"n": 1, "section": "Pasal 19 ayat (5) huruf f (as replaced by Permenkumham 11/2024 Pasal I angka 6)", "text": "untuk kegiatan melakukan seni dan budaya, berupa: 1. undangan dari penyelenggara kegiatan, bagi pelaku kegiatan seni dan budaya yang bersifat umum; atau 2. permohonan Visa dari impresariat dan kontrak kerja sama penampil (performer) dengan penyelenggara kegiatannya, bagi penampil (performer) musik atau pendukungnya;"}], "retrieved_at": "2026-10-18T18:40:59Z", "source": {"amendment": {"citation": "Permenkumham 11/2024 Pasal I angka 6 (Pasal 19 ayat (1) and ayat (5))", "document_sha256": "ecbbeccc196145f47f24e68473a27c19d6f0408e607abb365ce9acd22cbf5f02", "url": "https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf"}, "citation": "Permenkumham 22/2023 Pasal 13 ayat (1) huruf k", "classification_basis_citation": "Permenkumham 22/2023 Pasal 70 ayat (3)", "classification_basis_verbatim": "Ketentuan mengenai klasifikasi Visa sebagaimana dimaksud pada ayat (1) dan ayat (2) ditetapkan oleh Menteri.", "code_to_activity_status": "TO_VERIFY", "document_sha256": "2e9165804c38e9a60b4f1c4a046bd727a97043d7386be90d608c68db31c015d4", "fidelity": "VERBATIM_REGULATION_WHITESPACE_NORMALIZED", "index_evidence": {"retrieved_at": "2026-10-18T18:40:59Z", "url": "https://kanwilsultra.imigrasi.go.id/wna/daftar-visa-indonesia", "verbatim": "C7 Visa Kunjungan Penampilan Seni dan Budaya"}, "type": "REGULATION"}, "source_url": "https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf", "sponsor_required": {"as_stated_boolean": null, "invitation_requirement_see": "requirements_verbatim", "verbatim": ["bukti penjaminan dari Penjamin, kecuali untuk kunjungan tertentu;"]}, "status": "REGULATION_SOURCED_TO_VERIFY", "stay_days": null, "to_verify": ["source.code_to_activity_status", "statutory_parent_child_relationship", "page_sha256", "stay_days", "extendable", "where_to_apply", "sponsor_required.as_stated_boolean", "complete_code_specific_requirements", "prohibited_or_conditions_verbatim", "ketentuan_lain_verbatim", "pnbp_verbatim_for_reference_only", "is_sold", "price_keys"], "where_to_apply": null}$j$::jsonb,
    'https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf',
    NULL,  -- pragma: allowlist secret
    '2026-10-18T18:40:59Z'::timestamptz,
    'bafe7b885bc29abd81b48ea5a42478da90e8d42161c4e50d7353bd289ef2aa2a',  -- pragma: allowlist secret
    'TO_VERIFY',
    NULL,
    NULL,
    NULL
)
ON CONFLICT (code) DO UPDATE SET
    family = EXCLUDED.family,
    official_name_id = EXCLUDED.official_name_id,
    permit_class = EXCLUDED.permit_class,
    is_sold = EXCLUDED.is_sold,
    price_keys = EXCLUDED.price_keys,
    entry_type = EXCLUDED.entry_type,
    sponsor = EXCLUDED.sponsor,
    apply_channel = EXCLUDED.apply_channel,
    initial_stay_days = EXCLUDED.initial_stay_days,
    extendable = EXCLUDED.extendable,
    max_total_days = EXCLUDED.max_total_days,
    max_total_months = EXCLUDED.max_total_months,
    itap = EXCLUDED.itap,
    law_card = EXCLUDED.law_card,
    source_url = EXCLUDED.source_url,
    page_sha256 = EXCLUDED.page_sha256,
    retrieved_at = EXCLUDED.retrieved_at,
    card_sha256 = EXCLUDED.card_sha256,
    verification_status = EXCLUDED.verification_status,
    valid_from = EXCLUDED.valid_from,
    valid_until = EXCLUDED.valid_until,
    superseded_by = EXCLUDED.superseded_by;

INSERT INTO visa_types (code, name, category, family, official_name_id, permit_class, is_sold, price_keys, entry_type, sponsor, apply_channel, initial_stay_days, extendable, max_total_days, max_total_months, itap, law_card, source_url, page_sha256, retrieved_at, card_sha256, verification_status, valid_from, valid_until, superseded_by)
VALUES (
    'C8',
    'C8 Visa Kunjungan Kegiatan Olahraga',
    'Visit Visa',
    'C',
    'C8 Visa Kunjungan Kegiatan Olahraga',
    'VISIT_SINGLE',
    NULL,
    $j$[]$j$::jsonb,
    'SINGLE',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    $j${"allowed_activities": "see definition_verbatim", "code": "C8", "definition_verbatim": "olahraga yang tidak bersifat komersial;", "entry": "single", "extendable": null, "jenis_visa_verbatim": "Visa kunjungan 1 (satu) kali perjalanan dapat diberikan kepada Orang Asing untuk melakukan kegiatan:", "ketentuan_lain_verbatim": null, "notes": ["The official index lists this code; the regulations name activities, not these alphanumeric indices. The title comes from the official index, not from the regulation. Code-to-activity mapping and any statutory parent/child relationship remain TO_VERIFY pending the classification decision.", "Only whitespace is normalized in regulatory excerpts. Pasal 19 excerpts are activity-level evidence, not a complete visa-specific checklist. No eligibility, stay, extension, sponsorship exception or price is inherited from a child card.", "page_sha256 is null because no substantive code-specific page was retrieved. PDF content hashes are recorded separately in source; the DB verification_status remains TO_VERIFY."], "official_name_id": "C8 Visa Kunjungan Kegiatan Olahraga", "page_sha256": null, "pengajuan_intro_verbatim": "Permohonan Visa kunjungan 1 (satu) kali perjalanan bagi Orang Asing diajukan melalui aplikasi dengan melampirkan:", "pnbp_verbatim_for_reference_only": null, "prohibited_or_conditions_verbatim": null, "requirements_verbatim": [{"n": 1, "section": "Pasal 19 ayat (5) huruf g (as replaced by Permenkumham 11/2024 Pasal I angka 6)", "text": "untuk kegiatan melakukan olahraga yang tidak bersifat komersil, berupa undangan dari penyelenggara kegiatan;"}], "retrieved_at": "2026-10-18T18:40:59Z", "source": {"amendment": {"citation": "Permenkumham 11/2024 Pasal I angka 6 (Pasal 19 ayat (1) and ayat (5))", "document_sha256": "ecbbeccc196145f47f24e68473a27c19d6f0408e607abb365ce9acd22cbf5f02", "url": "https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf"}, "citation": "Permenkumham 22/2023 Pasal 13 ayat (1) huruf l", "classification_basis_citation": "Permenkumham 22/2023 Pasal 70 ayat (3)", "classification_basis_verbatim": "Ketentuan mengenai klasifikasi Visa sebagaimana dimaksud pada ayat (1) dan ayat (2) ditetapkan oleh Menteri.", "code_to_activity_status": "TO_VERIFY", "document_sha256": "2e9165804c38e9a60b4f1c4a046bd727a97043d7386be90d608c68db31c015d4", "fidelity": "VERBATIM_REGULATION_WHITESPACE_NORMALIZED", "index_evidence": {"retrieved_at": "2026-10-18T18:40:59Z", "url": "https://kanwilsultra.imigrasi.go.id/wna/daftar-visa-indonesia", "verbatim": "C8 Visa Kunjungan Kegiatan Olahraga"}, "type": "REGULATION"}, "source_url": "https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf", "sponsor_required": {"as_stated_boolean": null, "invitation_requirement_see": "requirements_verbatim", "verbatim": ["bukti penjaminan dari Penjamin, kecuali untuk kunjungan tertentu;"]}, "status": "REGULATION_SOURCED_TO_VERIFY", "stay_days": null, "to_verify": ["source.code_to_activity_status", "statutory_parent_child_relationship", "page_sha256", "stay_days", "extendable", "where_to_apply", "sponsor_required.as_stated_boolean", "complete_code_specific_requirements", "prohibited_or_conditions_verbatim", "ketentuan_lain_verbatim", "pnbp_verbatim_for_reference_only", "is_sold", "price_keys"], "where_to_apply": null}$j$::jsonb,
    'https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf',
    NULL,  -- pragma: allowlist secret
    '2026-10-18T18:40:59Z'::timestamptz,
    'd12b4ace42e2cb0ab51b4b867d56bd4be4baf97182bbd60b4dddde4b3c415a08',  -- pragma: allowlist secret
    'TO_VERIFY',
    NULL,
    NULL,
    NULL
)
ON CONFLICT (code) DO UPDATE SET
    family = EXCLUDED.family,
    official_name_id = EXCLUDED.official_name_id,
    permit_class = EXCLUDED.permit_class,
    is_sold = EXCLUDED.is_sold,
    price_keys = EXCLUDED.price_keys,
    entry_type = EXCLUDED.entry_type,
    sponsor = EXCLUDED.sponsor,
    apply_channel = EXCLUDED.apply_channel,
    initial_stay_days = EXCLUDED.initial_stay_days,
    extendable = EXCLUDED.extendable,
    max_total_days = EXCLUDED.max_total_days,
    max_total_months = EXCLUDED.max_total_months,
    itap = EXCLUDED.itap,
    law_card = EXCLUDED.law_card,
    source_url = EXCLUDED.source_url,
    page_sha256 = EXCLUDED.page_sha256,
    retrieved_at = EXCLUDED.retrieved_at,
    card_sha256 = EXCLUDED.card_sha256,
    verification_status = EXCLUDED.verification_status,
    valid_from = EXCLUDED.valid_from,
    valid_until = EXCLUDED.valid_until,
    superseded_by = EXCLUDED.superseded_by;

-- === ROLLBACK ===

-- Clears only the Law columns of these codes; rows and legacy columns stay.
UPDATE visa_types SET
    family = NULL,
    official_name_id = NULL,
    permit_class = NULL,
    is_sold = NULL,
    price_keys = '[]'::jsonb,
    entry_type = NULL,
    sponsor = NULL,
    apply_channel = NULL,
    initial_stay_days = NULL,
    extendable = NULL,
    max_total_days = NULL,
    max_total_months = NULL,
    itap = NULL,
    law_card = NULL,
    source_url = NULL,
    page_sha256 = NULL,
    retrieved_at = NULL,
    card_sha256 = NULL,
    verification_status = 'TO_VERIFY',
    valid_from = NULL,
    valid_until = NULL,
    superseded_by = NULL
WHERE code IN ('C22', 'C7', 'C8');
