-- ============================================================
-- 366_related_evidence_objects.sql
-- The registry tables written by the older writers (361 subjects and identifiers,
-- 362 documents and subject-document links) hold `evidence` as a jsonb STRING
-- scalar whose text is a JSON object, not as the jsonb object the column is meant
-- to carry. Cause: the writers called json.dumps before binding to a ::jsonb
-- parameter, and the production asyncpg codec encodes a second time. The writers
-- are fixed in the same change; this migration repairs the rows already written
-- and forbids the wrong shape from now on.
-- Date: 2026-11-18
--
-- Forward, per table: (1) unwrap every string-typed evidence whose text is a
-- JSON object, with (evidence #>> '{}')::jsonb; (2) add CHECK
-- (jsonb_typeof(evidence) = 'object') as NOT VALID, then VALIDATE it, so the
-- ACCESS EXCLUSIVE window is the catalog change only and the scan runs under a
-- weaker lock. A row that is neither an object nor a string holding an object
-- is not touched by the UPDATE and makes VALIDATE fail loudly: the migration
-- aborts instead of guessing.
--
-- These tables are insert-only for the application (no route updates or deletes
-- a row). This one-time UPDATE is the gated way to repair rows the application
-- wrote in the wrong shape; it changes only the representation of evidence,
-- never its content, and touches no other column. 365's related_subject_names
-- already carries the same CHECK and is not involved.
--
-- Rollback drops the four constraints only. It does NOT turn objects back into
-- strings: the object shape is the intended one and nothing reads the string.
--
-- Consumer: services/crm/related_subjects.py and related_documents.py through
-- POST /api/crm/related-subjects/register and .../documents/register, and the
-- read routes of services/crm/related_registry_read.py.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

UPDATE related_subjects
   SET evidence = (evidence #>> '{}')::jsonb
 WHERE jsonb_typeof(evidence) = 'string' AND (evidence #>> '{}') IS JSON OBJECT;

UPDATE related_subject_identifiers
   SET evidence = (evidence #>> '{}')::jsonb
 WHERE jsonb_typeof(evidence) = 'string' AND (evidence #>> '{}') IS JSON OBJECT;

UPDATE related_documents
   SET evidence = (evidence #>> '{}')::jsonb
 WHERE jsonb_typeof(evidence) = 'string' AND (evidence #>> '{}') IS JSON OBJECT;

UPDATE related_subject_documents
   SET evidence = (evidence #>> '{}')::jsonb
 WHERE jsonb_typeof(evidence) = 'string' AND (evidence #>> '{}') IS JSON OBJECT;

ALTER TABLE related_subjects
    ADD CONSTRAINT related_subjects_evidence_object_chk
    CHECK (jsonb_typeof(evidence) = 'object') NOT VALID;
ALTER TABLE related_subjects VALIDATE CONSTRAINT related_subjects_evidence_object_chk;

ALTER TABLE related_subject_identifiers
    ADD CONSTRAINT related_subject_identifiers_evidence_object_chk
    CHECK (jsonb_typeof(evidence) = 'object') NOT VALID;
ALTER TABLE related_subject_identifiers VALIDATE CONSTRAINT related_subject_identifiers_evidence_object_chk;

ALTER TABLE related_documents
    ADD CONSTRAINT related_documents_evidence_object_chk
    CHECK (jsonb_typeof(evidence) = 'object') NOT VALID;
ALTER TABLE related_documents VALIDATE CONSTRAINT related_documents_evidence_object_chk;

ALTER TABLE related_subject_documents
    ADD CONSTRAINT related_subject_documents_evidence_object_chk
    CHECK (jsonb_typeof(evidence) = 'object') NOT VALID;
ALTER TABLE related_subject_documents VALIDATE CONSTRAINT related_subject_documents_evidence_object_chk;

-- === ROLLBACK ===

ALTER TABLE related_subjects DROP CONSTRAINT IF EXISTS related_subjects_evidence_object_chk;
ALTER TABLE related_subject_identifiers DROP CONSTRAINT IF EXISTS related_subject_identifiers_evidence_object_chk;
ALTER TABLE related_documents DROP CONSTRAINT IF EXISTS related_documents_evidence_object_chk;
ALTER TABLE related_subject_documents DROP CONSTRAINT IF EXISTS related_subject_documents_evidence_object_chk;
