-- =============================================================================
-- NUZANTARA CRM — DRIVE 4-FOLDER TAXONOMY & ALIASES (PostgreSQL)
-- Consensus: Astra (gpt-6-astra) · Fable (claude-fable-5-1) · Qwen (qwen3.8-max) · Muse (muse-wonder-1.0)
-- =============================================================================

-- 1. Mirror of client_drive_subfolders for PT folders (avoids re-listing 154 PTs x 62k files)
CREATE TABLE IF NOT EXISTS company_drive_subfolders (
    company_id      integer     NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    folder_name     text        NOT NULL,
    drive_folder_id text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, folder_name)
);

CREATE INDEX IF NOT EXISTS idx_company_drive_subfolders_folder_id
    ON company_drive_subfolders (drive_folder_id);

-- 2. Legacy -> Canonical alias table (keeps all 293 existing client_drive_subfolders working)
CREATE TABLE IF NOT EXISTS drive_taxonomy_aliases (
    entity_kind    text NOT NULL CHECK (entity_kind IN ('individual', 'company')),
    legacy_name    text NOT NULL,
    canonical_name text NOT NULL,
    PRIMARY KEY (entity_kind, legacy_name)
);

INSERT INTO drive_taxonomy_aliases (entity_kind, legacy_name, canonical_name) VALUES
    -- Individual 4-folder taxonomy
    ('individual', '00_Profile',     '01_Profile'),
    ('individual', '01_Immigration', '02_Immigration'),
    ('individual', '02_Company',     '03_Company_and_Tax'),
    ('individual', '03_Tax',         '03_Company_and_Tax'),
    ('individual', '03_Company&TAX', '03_Company_and_Tax'),
    ('individual', '04_Family',      '01_Profile'),
    ('individual', '99_Misc',        '04_Archive'),
    -- Company / PT 4-folder taxonomy
    ('company', '00_PROFIL_E_ FONDAMENTA_AZIENDALI',   '01_Legal_and_Licensing'),
    ('company', '00_PROFIL_E_FONDAMENTA_AZIENDALI',    '01_Legal_and_Licensing'),
    ('company', '03_DICHIARAZIONI_E_RICEVUTE_FISCALI', '02_Tax_Returns_and_Payments'),
    ('company', '04_BANCHE_E_TRANSAZIONI_FINANZIARIE', '03_Finance_and_Banking'),
    ('company', '02_CONTABILITA_E_BILANCI_STORICI',    '03_Finance_and_Banking'),
    ('company', '01_RISORSE_UMANE_E_IMMIGRAZIONE',     '99_Archive')
ON CONFLICT (entity_kind, legacy_name)
DO UPDATE SET canonical_name = EXCLUDED.canonical_name;

-- 3. Idempotent journal for personal document evacuations (PT -> Individual)
CREATE TABLE IF NOT EXISTS drive_personal_document_moves (
    source_company_document_id integer PRIMARY KEY,
    source_company_id          integer NOT NULL,
    destination_client_id      integer NOT NULL,
    drive_file_id              text    NOT NULL UNIQUE,
    destination_folder_id      text    NOT NULL,
    destination_document_type  text    NOT NULL,
    state                      text    NOT NULL DEFAULT 'completed',
    created_at                 timestamptz NOT NULL DEFAULT now()
);

-- === ROLLBACK ===
DROP TABLE IF EXISTS drive_personal_document_moves;
DROP TABLE IF EXISTS drive_taxonomy_aliases;
DROP TABLE IF EXISTS company_drive_subfolders;
