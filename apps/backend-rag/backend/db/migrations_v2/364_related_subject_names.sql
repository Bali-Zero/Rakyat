-- ============================================================
-- 364_related_subject_names.sql
-- Slice 4A of the related-subject registry: RECORDED NAMES. A name is a recorded
-- fact about a subject, with its source (OCR reading, corrected MRZ reading,
-- manual entry). It is added; nothing is overwritten, and related_subjects is
-- not touched. The name a reader sees is computed at read time from the
-- registration name and these rows by source rank.
-- Date: 2026-11-18
--
-- Foreign key points only at the registry table related_subjects (361). No
-- reference to any existing CRM table, no trigger, no view. Insert-only is a
-- restriction of the application, proved by a write measurement on PostgreSQL,
-- not enforced by this schema: there is no updated_at, and the route never
-- updates or deletes a row.
--
-- Identity of a recorded name is the triple (subject_id, source, name): the
-- same name under another source is another row. evidence is the producer's
-- assertion (plan hash and counts); it never holds a name, a path or text.
-- The CHECKs are weaker than the application's validity rule on purpose: they
-- accept some strings the route refuses (a letterless one), never the reverse.
-- 'manual' is admitted here for a later slice; this slice's route refuses it.
--
-- Consumer: services/crm/related_names.py through
-- POST /api/crm/related-subjects/names/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off), and the read routes of
-- services/crm/related_registry_read.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_names (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200 AND name = btrim(name)),
    source     TEXT NOT NULL CHECK (source IN ('ocr_corroborated', 'ocr_single', 'mrz_corrected', 'manual')),
    evidence   JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (subject_id, source, name)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_names_subject
    ON related_subject_names (subject_id);

COMMENT ON TABLE related_subject_names IS
    'Recorded names of registry subjects, one row per (subject, source, name), insert-only by application restriction. A name read by OCR is a lead, not a fact.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS related_subject_names;
