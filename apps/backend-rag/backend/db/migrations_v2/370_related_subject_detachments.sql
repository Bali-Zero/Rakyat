-- ============================================================
-- 370_related_subject_detachments.sql
-- Slice 4D of the related-subject registry: DETACHMENTS. A person record of the
-- registry that holds the national identity number (nik) of ANOTHER person, proven
-- by the birth date the number encodes, gives that one key, and the documents the
-- request lists, to a NEW person record. The key and the document links move by an
-- UPDATE of their subject_id; this migration adds the HISTORY of each detachment.
-- Date: 2026-11-19
--
-- related_subject_detachments is insert-only HISTORY: one row per new record
-- (to_id is UNIQUE), written in the same transaction as the move. from_id is NOT
-- unique (one record may give several keys, one request each). owner_keys_sha256 is
-- the lowercase hex sha256 of the submitted owner key set (canonical lines
-- 'type:norm', sorted by code point, joined by one newline, no final newline); with
-- moved_documents (the sorted content hashes moved) it identifies the request, so a
-- repeat answers already_detached. It is a digest of key values: never logged,
-- returned or audited. evidence is the producer's assertion (plan hash, a count);
-- it never holds a name, an identifier value, a path or a client name.
--
-- Both foreign keys point only at the registry table related_subjects (361) with ON
-- DELETE CASCADE: the operator's undo of a detachment deletes the new record and its
-- history row goes with it.
--
-- The rollback section removes the table this file creates and nothing else; it does
-- not move a key or a document back (that is the operator's undo, not a rollback).
--
-- Consumer: services/crm/related_detach.py through
-- POST /api/crm/related-subjects/identifiers/detach (flag
-- RELATED_SUBJECT_REGISTRY_ENABLED, default off).
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_detachments (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_id           BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    to_id             BIGINT NOT NULL UNIQUE REFERENCES related_subjects(id) ON DELETE CASCADE,
    id_type           TEXT NOT NULL
                      CHECK (id_type IN ('passport', 'permit', 'nik', 'npwp', 'nib', 'ahu')),
    owner_keys_sha256 TEXT NOT NULL CHECK (owner_keys_sha256 ~ '^[0-9a-f]{64}$'),
    moved_documents   TEXT[] NOT NULL,
    basis             TEXT NOT NULL CHECK (basis IN ('nik_birth_date')),
    evidence          JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (from_id <> to_id)
);

CREATE INDEX IF NOT EXISTS idx_related_subject_detachments_from
    ON related_subject_detachments (from_id);

COMMENT ON TABLE related_subject_detachments IS
    'History of detachments of registry subjects (one key and listed documents moved from from_id to the new record to_id), insert-only by application restriction. owner_keys_sha256 is a digest of key values: never logged, returned or audited.';

-- === ROLLBACK ===
