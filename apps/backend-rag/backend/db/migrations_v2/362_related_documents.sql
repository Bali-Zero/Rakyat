-- ============================================================
-- 362_related_documents.sql
-- Slice 2 of the related-subject registry: documents BY REFERENCE. A document
-- is a content hash, the Drive file ids where that content was asserted, and
-- links to the registry subjects it belongs to. No file is written anywhere;
-- every value here is a producer ASSERTION that the route never verifies.
-- Date: 2026-11-17
--
-- Foreign keys point only at registry tables (related_subjects from 361 and
-- the two tables below). No reference to any existing CRM table, no trigger,
-- no view. Insert-only is a restriction of the application, proved by a write
-- measurement on PostgreSQL, not enforced by this schema: there is no
-- updated_at, and the route never updates or deletes a row.
--
-- A sighting is one retained observation per (kind, ref, sha256): it carries
-- no recency, and no reader may derive "the current content of a Drive file"
-- from these rows. first_seen_on / first_channel are the FIRST ACCEPTED
-- assertions for a hash, not a guaranteed earliest observation. doc_type is an
-- immutable provisional label; only its shape is checked here.
--
-- Consumer: services/crm/related_documents.py through
-- POST /api/crm/related-subjects/documents/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off), fed by
-- scripts/related_subject_documents_apply.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_documents (
    sha256        TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size     BIGINT NOT NULL CHECK (byte_size > 0),
    mime_type     TEXT NOT NULL CHECK (mime_type ~ '^[a-z0-9][a-z0-9.+-]{0,60}/[a-z0-9][a-z0-9.+-]{0,90}$'),
    first_seen_on DATE NOT NULL,
    first_channel TEXT NOT NULL CHECK (first_channel IN ('drive', 'whatsapp')),
    evidence      JSONB NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS related_document_sightings (
    kind       TEXT NOT NULL CHECK (kind IN ('drive')),
    ref        TEXT NOT NULL CHECK (ref ~ '^[A-Za-z0-9_-]{20,80}$'),
    sha256     TEXT NOT NULL REFERENCES related_documents(sha256) ON DELETE CASCADE,
    seen_on    DATE NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, ref, sha256)
);

CREATE INDEX IF NOT EXISTS idx_related_document_sightings_sha256
    ON related_document_sightings (sha256);

CREATE TABLE IF NOT EXISTS related_subject_documents (
    subject_id BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    sha256     TEXT NOT NULL REFERENCES related_documents(sha256),
    doc_type   TEXT NOT NULL CHECK (doc_type ~ '^[a-z][a-z0-9_]{1,39}$'),
    evidence   JSONB NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (subject_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_documents_sha256
    ON related_subject_documents (sha256);

COMMENT ON TABLE related_documents IS
    'Documents by reference: one row per content hash, producer assertions only (size, mime, first sighting). The route never sees the bytes.';
COMMENT ON TABLE related_document_sightings IS
    'Drive file ids where a content hash was asserted. One retained observation per (kind, ref, sha256); carries no recency.';
COMMENT ON TABLE related_subject_documents IS
    'Link of a registry subject to a document, with the producer''s provisional doc_type label. Insert-only by application restriction.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS related_subject_documents;
DROP TABLE IF EXISTS related_document_sightings;
DROP TABLE IF EXISTS related_documents;
