-- ============================================================
-- 369_related_subject_merges.sql
-- Slice 4C of the related-subject registry: MERGES. Two records of the registry that
-- are the same person or company become one for every reader by a pointer,
-- related_subjects.merged_into (361, set once from NULL to the survivor's id on the
-- merged-away record). Nothing moves and nothing is removed. The pointer is the single
-- truth; this migration adds its HISTORY (what was merged, when, on which basis) and the
-- index the read path needs to find "the records merged into S".
-- Date: 2026-11-19
--
-- related_subject_merges is insert-only HISTORY: whether a record is merged is decided by
-- related_subjects.merged_into alone, never by this table. source_id is NOT unique (a
-- source may have several rows after an operator clears its pointer and merges it again).
-- Both foreign keys point only at the registry table related_subjects (361). Depth one,
-- same kind and absence of a cycle are rules of the application, not of this schema.
-- evidence is the producer's assertion (plan hash, a count); it never holds a name, an
-- identifier value, a path or a client name. 'same_identity' and 'manual' are admitted
-- here for a later slice.
--
-- The rollback section removes the two objects this file creates (the history table and
-- the partial index) and nothing else: it does not touch the column merged_into, its
-- foreign key or CHECK, and does not clear a pointer. After a rollback the pointers stay
-- in force without their history; a reader that joins this table must be rolled back
-- with it.
--
-- Consumer: services/crm/related_registry_read.py through the read routes of
-- app/routers/crm_related_registry_read.py (a survivor shows the union of the records
-- merged into it), and services/crm/related_merges.py through
-- POST /api/crm/related-subjects/merges/register (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off) in the following pull request.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_merges (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id  BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    target_id  BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    basis      TEXT NOT NULL CHECK (basis IN ('permit_stamp', 'same_identity', 'manual')),
    evidence   JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (source_id <> target_id)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_merges_source
    ON related_subject_merges (source_id);

CREATE INDEX IF NOT EXISTS idx_related_subject_merges_target
    ON related_subject_merges (target_id);

CREATE INDEX IF NOT EXISTS idx_related_subjects_merged_into
    ON related_subjects (merged_into) WHERE merged_into IS NOT NULL;

COMMENT ON TABLE related_subject_merges IS
    'History of merges of registry subjects (source merged into target), insert-only by application restriction. The pointer related_subjects.merged_into is the single truth; this table says when and on which basis.';
