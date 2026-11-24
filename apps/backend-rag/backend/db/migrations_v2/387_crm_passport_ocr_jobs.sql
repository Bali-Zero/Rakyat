-- An explicit pending transition enrolls new work. Existing rows are opt-in.
SET LOCAL lock_timeout = '5s';
CREATE TABLE crm_passport_ocr_jobs (
    document_id bigint PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    client_id bigint NOT NULL,
    file_id text NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','leased','completed','review')),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'leased') = (lease_token IS NOT NULL)),
    CHECK ((status = 'review') = (terminal_reason IS NOT NULL))
);
CREATE INDEX crm_passport_ocr_ready ON crm_passport_ocr_jobs (available_at, document_id)
    WHERE status IN ('pending','leased');
CREATE INDEX crm_passport_ocr_owner_by_file ON documents (file_id)
    WHERE deleted_at IS NULL AND status IS DISTINCT FROM 'deleted' AND NOT coalesce(is_archived,false);
CREATE INDEX crm_passport_ocr_owner_by_hash ON documents (content_hash)
    WHERE content_hash IS NOT NULL AND deleted_at IS NULL
      AND status IS DISTINCT FROM 'deleted' AND NOT coalesce(is_archived,false);

CREATE FUNCTION crm_passport_ocr_is_passport(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(btrim(coalesce(value,''), E' \t\r\n\f' || chr(11) || chr(160))) IN ('passport','paspor')
$$;

-- Surface externally owned intake records without changing their legacy state.
CREATE FUNCTION crm_passport_ocr_staff_status(d documents) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN d.ocr_status='pending' AND crm_passport_ocr_is_passport(d.document_type)
        AND (d.intake_idempotency_key IS NOT NULL OR d.intake_proposal_id IS NOT NULL)
        THEN 'intake' ELSE d.ocr_status END
$$;

-- A protected document must never receive an extraction or a review marker from this worker.
CREATE FUNCTION crm_passport_ocr_writable(d documents) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT crm_passport_ocr_is_passport(d.document_type)
       AND d.ocr_status = 'pending' AND d.status IN ('active','google_drive')
       AND d.deleted_at IS NULL AND NOT coalesce(d.is_archived, false)
       AND d.verified_at IS NULL AND d.verified_by IS NULL AND d.rejection_reason IS NULL
       AND d.ocr_completed_at IS NULL
       AND d.intake_idempotency_key IS NULL AND d.intake_proposal_id IS NULL
       AND coalesce(d.ocr_extracted_data, '{}'::jsonb) IN ('{}'::jsonb, 'null'::jsonb)
$$;

CREATE FUNCTION crm_passport_ocr_review_code(d documents) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN d.storage_type IS DISTINCT FROM 'google_drive' OR nullif(btrim(d.file_id),'') IS NULL
             OR d.file_id !~ '^[A-Za-z0-9_-]+$' THEN 'unsupported_file_reference'
        WHEN d.family_member_id IS NOT NULL THEN 'family_document'
        WHEN NOT EXISTS (SELECT FROM clients c WHERE c.id=d.client_id AND c.status='active'
                         AND c.deleted_at IS NULL) THEN 'passport_ocr_inactive_client'
        WHEN EXISTS (SELECT FROM documents x WHERE x.client_id <> d.client_id AND x.deleted_at IS NULL
                     AND x.status IS DISTINCT FROM 'deleted' AND NOT coalesce(x.is_archived,false)
                     AND (x.file_id=d.file_id OR x.content_hash=nullif(d.content_hash,'')))
             THEN 'ambiguous_document_binding'
    END
$$;

CREATE FUNCTION crm_passport_ocr_enqueue_pending() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE reason text;
BEGIN
    IF NOT coalesce(crm_passport_ocr_writable(NEW),false) THEN RETURN NEW; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ocr_status = 'pending' AND
           (NEW.client_id,NEW.file_id,NEW.content_hash,NEW.document_type)
           IS NOT DISTINCT FROM (OLD.client_id,OLD.file_id,OLD.content_hash,OLD.document_type)
           THEN RETURN NEW; END IF;
    END IF;
    reason := crm_passport_ocr_review_code(NEW);
    IF NEW.client_id IS NOT NULL AND nullif(btrim(NEW.file_id),'') IS NOT NULL THEN
        INSERT INTO crm_passport_ocr_jobs AS j
            (document_id,client_id,file_id,content_hash,status,terminal_reason)
        VALUES (NEW.id,NEW.client_id,NEW.file_id,coalesce(NEW.content_hash,''),
                CASE WHEN reason IS NULL THEN 'pending' ELSE 'review' END,reason)
        ON CONFLICT (document_id) DO UPDATE SET client_id=excluded.client_id, file_id=excluded.file_id,
            content_hash=excluded.content_hash, status=excluded.status, terminal_reason=excluded.terminal_reason,
            attempts=0, lease_token=NULL, available_at=now(), updated_at=now()
        WHERE (j.client_id,j.file_id,j.content_hash)
              IS DISTINCT FROM (excluded.client_id,excluded.file_id,excluded.content_hash)
           OR j.status IN ('completed','review');
    END IF;
    IF reason IS NOT NULL THEN
        UPDATE documents SET ocr_status='review',ocr_completed_at=now(),
            ocr_extracted_data=jsonb_build_object('review_reason',reason) WHERE id=NEW.id;
    END IF;
    RETURN NEW;
END $$;

-- Existing document writers may enroll through the trigger without a grant to
-- claim jobs or alter their leases. Resolve tables in the migration schema.
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.crm_passport_ocr_enqueue_pending() SECURITY DEFINER SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;

CREATE TRIGGER crm_passport_ocr_enqueue_pending_trg
AFTER INSERT OR UPDATE OF ocr_status, client_id, file_id, content_hash, document_type ON documents
FOR EACH ROW EXECUTE FUNCTION crm_passport_ocr_enqueue_pending();
REVOKE ALL ON FUNCTION crm_passport_ocr_enqueue_pending() FROM PUBLIC;

-- Read-only CRM consumers need the same explicit table visibility as existing
-- data-plane objects.  The guarded grant keeps disposable schema tests portable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nuzantara_readonly') THEN
        GRANT SELECT ON TABLE crm_passport_ocr_jobs TO nuzantara_readonly;
    END IF;
END $$;

-- === ROLLBACK ===
-- Keep settled documents and refuse to discard the queue while it contains work or audit history.
LOCK TABLE crm_passport_ocr_jobs IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT FROM crm_passport_ocr_jobs) THEN
        RAISE EXCEPTION 'Preserve passport OCR jobs before reversing this migration';
    END IF;
END $$;
DROP TRIGGER crm_passport_ocr_enqueue_pending_trg ON documents;
DROP FUNCTION crm_passport_ocr_enqueue_pending();
DROP FUNCTION crm_passport_ocr_review_code(documents);
DROP FUNCTION crm_passport_ocr_writable(documents);
DROP FUNCTION crm_passport_ocr_staff_status(documents);
DROP FUNCTION crm_passport_ocr_is_passport(text);
DROP INDEX crm_passport_ocr_owner_by_hash;
DROP INDEX crm_passport_ocr_owner_by_file;
DROP TABLE crm_passport_ocr_jobs;
