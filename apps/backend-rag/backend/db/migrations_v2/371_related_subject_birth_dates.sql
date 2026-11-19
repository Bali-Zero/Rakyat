-- ============================================================
-- 371_related_subject_birth_dates.sql
-- Slice 4E of the related-subject registry: BIRTH DATES. A person record of the
-- registry whose date_of_birth is NULL receives the birth date read from ONE identity
-- document already linked to that record (a passport data page, or an immigration
-- document that copies it: ITAS, e-visa). The date lands in the existing column
-- related_subjects.date_of_birth; this migration adds the HISTORY of each recording.
-- Date: 2026-11-19
--
-- related_subject_birth_dates is insert-only HISTORY: one row per record (subject_id
-- is UNIQUE: the route never overwrites a date). document_sha256 is the content hash
-- of the document the date was read from; there is deliberately no foreign key to the
-- document table, the route checks the link. evidence is the producer's assertion
-- (plan hash, a count); it never holds a name, an identifier value, a path or a
-- client name. The table is the one authorised place for the date and the hash: they
-- are never logged, returned or audited.
--
-- The foreign key points only at the registry table related_subjects (361) with ON
-- DELETE CASCADE.
--
-- The rollback section removes the table this file creates and nothing else; it does
-- not clear a date (that is the operator's undo, not a rollback).
--
-- Consumer: services/crm/related_birth_dates.py through
-- POST /api/crm/related-subjects/birth-date/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off).
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_birth_dates (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id      BIGINT NOT NULL UNIQUE REFERENCES related_subjects(id) ON DELETE CASCADE,
    birth_date      DATE NOT NULL CHECK (birth_date >= DATE '1900-01-01'),
    document_sha256 TEXT NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    document_kind   TEXT NOT NULL CHECK (document_kind IN ('passport_data_page', 'itas', 'evisa')),
    basis           TEXT NOT NULL CHECK (basis IN ('identity_document')),
    evidence        JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE related_subject_birth_dates IS
    'History of birth dates recorded on registry subjects from an identity document, insert-only by application restriction. Holds the date and the document hash: never logged, returned or audited.';

-- === ROLLBACK ===
