-- ============================================================
-- 372_related_subject_link_reviews.sql
-- Slice 4F of the related-subject registry: LINK REVIEWS. A CRM admin confirms (and says
-- how the subject relates to the client) or rejects a proposed link between a registry
-- subject and a client. related_subject_links keeps its rows and its relation exactly as
-- the producer wrote them; this table is the HISTORY of the human decisions.
-- Date: 2026-11-21
--
-- related_subject_link_reviews is insert-only by application rule, with no UNIQUE: a
-- decision is changed by a later row and the latest (created_at, id) for a subject and
-- a client is the effective one. client_id has no foreign key (the registry keeps no
-- reference to CRM tables, the rule of 361). decided_by is the deciding admin's email,
-- kept as the accountability of the decision.
--
-- The foreign key points only at the registry table related_subjects (361) with ON
-- DELETE CASCADE.
--
-- Consumer: services/crm/related_link_reviews.py through
-- POST /api/crm/related-registry/link-reviews and the detail read of
-- GET /api/crm/related-registry/{subject_id}.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS related_subject_link_reviews (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id  BIGINT NOT NULL REFERENCES related_subjects(id) ON DELETE CASCADE,
    client_id   INTEGER NOT NULL CHECK (client_id > 0),
    decision    TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
    relation    TEXT CHECK (relation IS NULL OR relation IN ('self', 'spouse', 'child', 'employee', 'other')),
    decided_by  TEXT NOT NULL CHECK (char_length(decided_by) BETWEEN 3 AND 254),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT related_subject_link_reviews_decision_relation_ck
        CHECK ((decision = 'confirmed') = (relation IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_related_subject_link_reviews_effective
    ON related_subject_link_reviews (subject_id, client_id, created_at DESC, id DESC);

COMMENT ON TABLE related_subject_link_reviews IS
    'History of human decisions on proposed registry links, insert-only by application rule. The latest (created_at, id) per subject and client is the effective review.';

-- === ROLLBACK ===
