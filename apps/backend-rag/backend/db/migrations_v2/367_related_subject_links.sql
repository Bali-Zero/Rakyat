-- ============================================================
-- 367_related_subject_links.sql
-- Slice 4B of the related-subject registry: PROPOSED CLIENT LINKS. A link records
-- that a registry subject probably belongs to a CRM client, with its basis (the
-- producer's assertion, never verified here) and the producer's evidence. A link
-- is a proposal: no confirmation, no relation editing, nothing shown to a client.
-- It is added; nothing is overwritten, and related_subjects is not touched.
-- Date: 2026-11-18
--
-- Foreign key points only at the registry table related_subjects (361).
-- client_id is a bare integer with NO foreign key to clients (the type of
-- clients.id): a client delete, a merge or a re-key in the CRM can neither be
-- blocked by, nor cascade into, the registry. A link can therefore outlive its
-- client; the read path shows that, and the write route checks the client at
-- write time. No trigger, no view. Insert-only is a restriction of the
-- application, proved by a write measurement on PostgreSQL, not enforced by this
-- schema: there is no updated_at, and the route never updates or deletes a row.
--
-- Identity of a link is the triple (subject_id, client_id, basis). evidence is the
-- producer's assertion (plan hash and a count); it never holds a name, an
-- identifier value, a path or a client name. 'manual' (basis) and every relation
-- other than 'unknown' are admitted here for a later slice; this slice's route
-- refuses 'manual' and takes the default relation for every row.
--
-- Consumer: services/crm/related_links.py through
-- POST /api/crm/related-subjects/links/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off), and the read routes of
-- services/crm/related_registry_read.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_links (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    client_id  INTEGER NOT NULL CHECK (client_id > 0),
    basis      TEXT NOT NULL CHECK (basis IN ('drive_folder', 'whatsapp_phone', 'identity', 'manual')),
    relation   TEXT NOT NULL DEFAULT 'unknown' CHECK (relation IN (
        'unknown', 'self', 'spouse', 'child', 'parent', 'sibling', 'other_family',
        'director', 'commissioner', 'shareholder', 'employee', 'sponsor')),
    evidence   JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (subject_id, client_id, basis)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_links_subject
    ON related_subject_links (subject_id);

CREATE INDEX IF NOT EXISTS idx_related_subject_links_client
    ON related_subject_links (client_id);

COMMENT ON TABLE related_subject_links IS
    'Proposed client links of registry subjects, one row per (subject, client, basis), insert-only by application restriction. A link is a proposal from documents, not a confirmed relation.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS related_subject_links;
