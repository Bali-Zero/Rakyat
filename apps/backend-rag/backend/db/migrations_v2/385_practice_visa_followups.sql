-- Day zero is the first recorded upload, in Bali calendar days. Keep this
-- independent of the official issue date printed on the visa.
CREATE TABLE practice_visa_followups (
    practice_id integer PRIMARY KEY REFERENCES practices(id) ON DELETE RESTRICT,
    service_code text NOT NULL,
    slot_key text NOT NULL,
    started_on date NOT NULL,
    due_on date NOT NULL,
    allow_postpone boolean NOT NULL,
    fulfilled_at timestamptz,
    postponed_at timestamptz,
    postponed_by text,
    postponement_note text,
    escalation_note text,
    email_state text NOT NULL DEFAULT 'not_sent'
        CHECK(email_state IN ('not_sent','sending','failed','uncertain','sent')),
    email_idempotency_key uuid NOT NULL DEFAULT gen_random_uuid(),
    email_requested_at timestamptz,
    email_requested_by text,
    email_last_attempt_at timestamptz,
    email_attempts integer NOT NULL DEFAULT 0 CHECK(email_attempts >= 0),
    email_sent_at timestamptz,
    owner_release_at timestamptz,
    owner_release_by text,
    owner_release_note text,
    CHECK ((postponed_at IS NULL AND postponed_by IS NULL AND postponement_note IS NULL AND due_on=started_on+30)
        OR (allow_postpone AND postponed_at IS NOT NULL AND postponed_by IS NOT NULL
            AND postponement_note IS NOT NULL AND due_on=started_on+60
            AND (postponed_at AT TIME ZONE 'Asia/Makassar')::date BETWEEN started_on+30 AND started_on+59)),
    CHECK ((email_state='sent') = (email_sent_at IS NOT NULL)),
    CHECK ((owner_release_at IS NULL) = (owner_release_by IS NULL))
);
CREATE INDEX idx_practice_visa_followups_due ON practice_visa_followups(due_on,practice_id)
    WHERE fulfilled_at IS NULL AND owner_release_at IS NULL;

CREATE TABLE practice_visa_followup_send_attempts (
    practice_id integer NOT NULL REFERENCES practice_visa_followups(practice_id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    requested_by text NOT NULL,
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'sending' CHECK(outcome IN ('sending','failed','uncertain','sent')),
    PRIMARY KEY(practice_id,attempt)
);

CREATE FUNCTION refresh_practice_visa_followup(case_id integer) RETURNS void
LANGUAGE sql AS $$
    WITH validity AS (
        SELECT f.practice_id, EXISTS(
            SELECT 1 FROM practice_delivery_slots s
            JOIN practices p ON p.id=s.practice_id
            JOIN documents d ON d.id=s.document_id
            WHERE s.practice_id=f.practice_id AND s.service_code=f.service_code
              AND s.slot_key=f.slot_key AND d.practice_id=p.id AND d.client_id=p.client_id
              AND d.family_member_id IS NOT DISTINCT FROM p.family_member_id
              AND NULLIF(d.file_id,'') IS NOT NULL AND d.deleted_at IS NULL
              AND NOT COALESCE(d.is_archived,false) AND d.status IS DISTINCT FROM 'deleted'
        ) AS ready FROM practice_visa_followups f WHERE f.practice_id=case_id
    )
    UPDATE practice_visa_followups f SET fulfilled_at=CASE WHEN validity.ready THEN now() ELSE NULL END
    FROM validity WHERE f.practice_id=validity.practice_id
      AND validity.ready IS DISTINCT FROM (f.fulfilled_at IS NOT NULL);
$$;

CREATE FUNCTION sync_visa_followup_slot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN PERFORM refresh_practice_visa_followup(OLD.practice_id);
    ELSE
        IF TG_OP='UPDATE' AND OLD.practice_id<>NEW.practice_id THEN
            PERFORM refresh_practice_visa_followup(OLD.practice_id);
        END IF;
        PERFORM refresh_practice_visa_followup(NEW.practice_id);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER visa_followup_slot_changed
AFTER INSERT OR DELETE OR UPDATE OF document_id,practice_id,slot_key,service_code
ON practice_delivery_slots FOR EACH ROW EXECUTE FUNCTION sync_visa_followup_slot();

CREATE FUNCTION sync_visa_followup_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.practice_id IS NOT NULL THEN PERFORM refresh_practice_visa_followup(OLD.practice_id); END IF;
    IF NEW.practice_id IS DISTINCT FROM OLD.practice_id AND NEW.practice_id IS NOT NULL THEN
        PERFORM refresh_practice_visa_followup(NEW.practice_id);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER visa_followup_document_changed
AFTER UPDATE OF status,is_archived,deleted_at,file_id,client_id,practice_id,family_member_id ON documents
FOR EACH ROW EXECUTE FUNCTION sync_visa_followup_document();

-- Reconstruct any pre-existing recorded issuance. Historical completed
-- cases with no authenticated delivery receipt do not acquire a new deadline.
INSERT INTO practice_visa_followups(practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
SELECT e.practice_id,e.service_code,CASE WHEN pt.category='kitas' THEN 'document_3' ELSE 'document_2' END,
       (e.recorded_at AT TIME ZONE 'Asia/Makassar')::date,
       (e.recorded_at AT TIME ZONE 'Asia/Makassar')::date + 30,
       pt.category<>'kitas' OR right(e.service_code,9)='_offshore'
FROM hr_delivery_bonus_events e
JOIN practices p ON p.id=e.practice_id
JOIN practice_types pt ON pt.id=p.practice_type_id
WHERE e.event_kind='electronic_visa_issued' AND pt.category IN ('single_entry_visa','multiple_entry_visa','kitas');
SELECT refresh_practice_visa_followup(practice_id) FROM practice_visa_followups;

-- === ROLLBACK ===
DROP TRIGGER visa_followup_document_changed ON documents;
DROP FUNCTION sync_visa_followup_document();
DROP TRIGGER visa_followup_slot_changed ON practice_delivery_slots;
DROP FUNCTION sync_visa_followup_slot();
DROP FUNCTION refresh_practice_visa_followup(integer);
DROP TABLE practice_visa_followup_send_attempts;
DROP TABLE practice_visa_followups;
