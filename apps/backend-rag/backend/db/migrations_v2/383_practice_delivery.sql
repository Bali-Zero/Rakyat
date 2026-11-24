-- Final outputs belong to a practice and keep their client/family identity in documents.
CREATE TABLE practice_delivery_titles (
    practice_id integer PRIMARY KEY REFERENCES practices(id) ON DELETE CASCADE,
    service_code text NOT NULL,
    titles text[] NOT NULL CHECK (cardinality(titles) BETWEEN 1 AND 30)
);

CREATE TABLE practice_delivery_slots (
    practice_id integer NOT NULL REFERENCES practices(id) ON DELETE CASCADE,
    service_code text NOT NULL,
    slot_key text NOT NULL,
    document_id integer REFERENCES documents(id) ON DELETE CASCADE,
    company_id integer REFERENCES companies(id) ON DELETE CASCADE,
    company_document_id integer REFERENCES company_documents(id) ON DELETE CASCADE,
    waiver_reason text,
    issued_on date,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, slot_key),
    CHECK ((document_id IS NULL) <> (waiver_reason IS NULL)),
    CHECK ((company_id IS NULL) = (company_document_id IS NULL))
);

-- Preserve the first accepted role of an output, even if its document is
-- later replaced, archived or removed. A combined permit is shared only by
-- explicitly referencing the accepted document from its compatible box.
CREATE TABLE practice_delivery_fingerprints (
    practice_id integer NOT NULL REFERENCES practices(id) ON DELETE CASCADE,
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    service_code text NOT NULL,
    slot_key text NOT NULL,
    PRIMARY KEY (practice_id, sha256)
);

CREATE TABLE practice_delivery_completions (
    practice_id integer PRIMARY KEY REFERENCES practices(id) ON DELETE CASCADE,
    service_code text NOT NULL,
    delivered_by text NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT now()
);

-- The issuing client, covered family member and commissioned service are part
-- of the stored evidence. This rule also covers importers and batch writers.
CREATE FUNCTION guard_practice_delivery_subject() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM practice_delivery_titles WHERE practice_id = OLD.id)
       OR EXISTS (SELECT 1 FROM practice_delivery_slots WHERE practice_id = OLD.id)
       OR EXISTS (SELECT 1 FROM practice_delivery_fingerprints WHERE practice_id = OLD.id)
       OR EXISTS (SELECT 1 FROM practice_delivery_completions WHERE practice_id = OLD.id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            CONSTRAINT = 'practice_delivery_subject_immutable',
            MESSAGE = 'Cannot change the subject or service of a practice with delivery evidence';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER practice_delivery_subject_guard
    BEFORE UPDATE OF client_id, family_member_id, practice_type_id ON practices
    FOR EACH ROW
    WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id
          OR OLD.family_member_id IS DISTINCT FROM NEW.family_member_id
          OR OLD.practice_type_id IS DISTINCT FROM NEW.practice_type_id)
    EXECUTE FUNCTION guard_practice_delivery_subject();

-- === ROLLBACK ===
LOCK TABLE practice_delivery_titles, practice_delivery_slots, practice_delivery_fingerprints, practice_delivery_completions IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM practice_delivery_titles) OR
       EXISTS (SELECT 1 FROM practice_delivery_slots) OR
       EXISTS (SELECT 1 FROM practice_delivery_fingerprints) OR
       EXISTS (SELECT 1 FROM practice_delivery_completions) THEN
        RAISE EXCEPTION 'Preserve recorded practice evidence before rollback of migration 383';
    END IF;
END $$;
DROP TRIGGER practice_delivery_subject_guard ON practices;
DROP FUNCTION guard_practice_delivery_subject();
DROP TABLE practice_delivery_completions;
DROP TABLE practice_delivery_fingerprints;
DROP TABLE practice_delivery_slots;
DROP TABLE practice_delivery_titles;
