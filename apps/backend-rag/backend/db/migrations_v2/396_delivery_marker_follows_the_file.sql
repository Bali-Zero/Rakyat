-- Bonus reform B2b, step A (PR-2). A delivery document's `case-delivery:<practice>:<company>:<slot>:<sha256>`
-- idempotency key names the SHA-256 of the bytes it was uploaded with, and the delivery card trusts that
-- digest to say which file a slot holds. The generic documents PATCH (and any other writer) can still point
-- such a document at another Drive file while keeping its key and content_hash, so the digest would certify
-- bytes the document no longer holds. This trigger makes the marker follow the file at the entity: when a
-- marked document's file_id, file_url, google_drive_file_url or content_hash changes and the writer did not
-- set a new key itself, the key is cleared (and an unchanged content_hash with it: it described the old
-- bytes). Nothing is refused, so staff can still repair a broken Drive link. Effect: the slot's
-- content_sha256 reads NULL (the card never confirms), and a replay of the original file no longer finds the
-- document by its marker, so it is staged again and lands as a replacement.
--
-- A pointer that is lost (file_id set to NULL or empty) is not a redirect to other bytes: the document stops
-- being live (a slot document needs a file_id), the delivery repair path relies on its marker and hash to
-- recognise it, and the trigger leaves it alone. Pointing it at a file again later does clear the marker.
--
-- Interaction with the other triggers on documents: this one is BEFORE and named so it runs first among the
-- BEFORE triggers; update_documents_updated_at only sets updated_at. The AFTER triggers
-- visa_followup_document_changed (reads status, is_archived, deleted_at, file_id, client_id, practice_id,
-- family_member_id: never the key) and crm_passport_ocr_enqueue_pending_trg (passport documents only) see the
-- final row and are unaffected. The delivery code never updates the address of a marked document (it only
-- sets client_visible, updated_at and archive flags), so nothing it does fires this trigger.
-- Release window: this runs in release_command before the rolling deploy, with the old image still serving.
-- The old image never reads the digest and never changes a marked document's address, so its behaviour does
-- not change. Rollback is safe at any time: it drops the trigger and the function; keys already cleared stay
-- cleared (a cleared key only means "no digest").

CREATE FUNCTION documents_delivery_marker_follows_file() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.intake_idempotency_key IS NOT DISTINCT FROM OLD.intake_idempotency_key
       AND NULLIF(NEW.file_id, '') IS NOT NULL
       AND (NEW.file_id IS DISTINCT FROM OLD.file_id
            OR NEW.file_url IS DISTINCT FROM OLD.file_url
            OR NEW.google_drive_file_url IS DISTINCT FROM OLD.google_drive_file_url
            OR NEW.content_hash IS DISTINCT FROM OLD.content_hash) THEN
        NEW.intake_idempotency_key := NULL;
        IF NEW.content_hash IS NOT DISTINCT FROM OLD.content_hash THEN
            NEW.content_hash := NULL;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- Created only where every column it names exists (a partial documents table must not break its writers).
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'documents'
          AND column_name IN ('intake_idempotency_key', 'file_id', 'file_url',
                              'google_drive_file_url', 'content_hash')) = 5 THEN
        CREATE TRIGGER documents_delivery_marker_follows_file
        BEFORE UPDATE ON documents FOR EACH ROW
        WHEN (OLD.intake_idempotency_key LIKE 'case-delivery:%')
        EXECUTE FUNCTION documents_delivery_marker_follows_file();
    END IF;
END $$;

-- === ROLLBACK ===
-- Safe at any time (see the header).
DROP TRIGGER IF EXISTS documents_delivery_marker_follows_file ON documents;
DROP FUNCTION IF EXISTS documents_delivery_marker_follows_file();
