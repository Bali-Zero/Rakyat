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

-- === ROLLBACK ===
DROP TABLE practice_delivery_completions;
DROP TABLE practice_delivery_fingerprints;
DROP TABLE practice_delivery_slots;
DROP TABLE practice_delivery_titles;
