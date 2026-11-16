-- ============================================================
-- 361_related_subjects_registry.sql
-- Registry of related subjects: people and companies whose documents are in
-- the intake backlog but who have no CRM card (workers, family members,
-- shareholders of client companies). They live OUTSIDE clients, documents and
-- client_family_members: code that counts, lists, notifies or provisions
-- clients reads tables these rows are not in.
-- Date: 2026-11-16
--
-- Slice 1 writes a row once and never updates it. No reference to any
-- existing table: no foreign key, no trigger, no view. merged_into is
-- reserved and always NULL in slice 1.
--
-- Invariant: ONE OWNER PER CANONICAL IDENTIFIER KEY, enforced by the primary
-- key (id_type, id_norm). id_norm follows norm_version 1 of
-- services/crm/related_identifiers.py.
--
-- Consumer: services/crm/related_subjects.py through
-- POST /api/crm/related-subjects/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off), fed by
-- scripts/related_subjects_apply.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subjects (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('person', 'company')),
    display_name  TEXT,
    date_of_birth DATE,
    nationality   TEXT,
    sex           TEXT,
    merged_into   BIGINT REFERENCES related_subjects(id),
    evidence      JSONB NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (merged_into IS NULL OR merged_into <> id)
);

CREATE TABLE IF NOT EXISTS related_subject_identifiers (
    id_type      TEXT NOT NULL CHECK (id_type IN ('passport', 'permit', 'nik', 'npwp', 'nib', 'ahu')),
    id_norm      TEXT NOT NULL CHECK (length(id_norm) >= 4),
    id_raw       TEXT NOT NULL,
    norm_version SMALLINT NOT NULL,
    subject_id   BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    evidence     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id_type, id_norm)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_identifiers_subject
    ON related_subject_identifiers (subject_id);

COMMENT ON TABLE related_subjects IS
    'People and companies with documents but no CRM card. Names and numbers in clear, same class of data as clients. Never read by a portal route.';
COMMENT ON TABLE related_subject_identifiers IS
    'Validated identifier keys of related_subjects. PRIMARY KEY (id_type, id_norm) = one owner per canonical key.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS related_subject_identifiers;
DROP TABLE IF EXISTS related_subjects;
