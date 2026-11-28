-- 397: practice document requests. A consultant asks the client for one more document on a practice
-- and says why (spec BLOCK-1-VISA section 4, slice V2). One row records who asked, why, and what the
-- reviewers later decide. New table only; no ALTER on practices, practice_types or
-- practice_required_documents.
-- required_document_id and practice_type_id are plain integers, deliberately not foreign keys: the
-- database the hosted job builds has no practice_required_documents table, and a practice type may
-- be deleted while its requests stay as history. A reader of this table must LEFT JOIN.
-- reason is for the reviewers and is never shown to the client.
-- Nothing reads decision, decision_condition, decided_by or decided_at yet (the review page is the
-- next slice).

CREATE TABLE practice_document_requests (
    id bigserial PRIMARY KEY,
    practice_id integer NOT NULL REFERENCES practices(id) ON DELETE CASCADE,
    required_document_id integer,
    practice_type_id integer,
    document_type varchar(50) NOT NULL,
    label varchar(100) NOT NULL,
    client_text text NOT NULL,
    reason text NOT NULL,
    requested_by varchar(255) NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    decision text NOT NULL DEFAULT 'pending'
        CHECK (decision IN ('pending', 'added_to_card', 'case_only')),
    decision_condition text,
    decided_by varchar(255),
    decided_at timestamptz,
    reviewers_notified_at timestamptz
);

CREATE INDEX idx_practice_document_requests_practice
    ON practice_document_requests (practice_id);
CREATE INDEX idx_practice_document_requests_seen
    ON practice_document_requests (practice_type_id, document_type, requested_at);

-- Read-only consumers see the new table; guarded as in 393 and 396.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nuzantara_readonly') THEN
        GRANT SELECT ON TABLE practice_document_requests TO nuzantara_readonly;
    END IF;
END $$;

-- === ROLLBACK ===
DROP TABLE practice_document_requests;
