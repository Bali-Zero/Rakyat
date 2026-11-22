-- ============================================================
-- 377_minor_note_once.sql
-- The trigger trg_check_minor (BEFORE INSERT OR UPDATE ON clients) appended one more
-- "[MINORE] Verificare collegamento a profilo genitore. Età: N anni." line to clients.notes
-- on EVERY update of a card whose date_of_birth is under 18 years: on 2026-11-22, 61 cards
-- carried 508 such lines, one card 158. The line was also glued to the end of the last
-- paragraph, so the WhatsApp recap guard of upsert-by-phone (#652), which compares the last
-- paragraph, could never see a repeated recap on those cards.
--
-- check_minor_client() now removes every "[MINORE] ..." line and writes ONE current line as
-- the first paragraph of the notes. Idempotent: a second update of the same card yields the
-- same notes. The 'minore' tag is kept as before. The function was created outside the
-- migration ledger; this file is its first versioned body.
--
-- Consumer: every UPDATE/INSERT on clients (Kita card edits, upsert-by-phone, intake writers).
-- Existing duplicated lines are normalised the next time each card is updated.
-- Date: 2026-11-22
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE OR REPLACE FUNCTION public.check_minor_client()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    rest text;
BEGIN
    IF NEW.date_of_birth IS NOT NULL AND
       DATE_PART('year', AGE(NEW.date_of_birth)) < 18 THEN
        rest := NULLIF(
            btrim(regexp_replace(COALESCE(NEW.notes, ''), '^\[MINORE\][^\n]*(\n|$)', '', 'gn'), E'\n'),
            ''
        );
        NEW.notes = '[MINORE] Verificare collegamento a profilo genitore. Età: ' ||
            DATE_PART('year', AGE(NEW.date_of_birth))::text || ' anni.' ||
            COALESCE(E'\n\n' || rest, '');
        IF NEW.tags IS NULL THEN
            NEW.tags = ARRAY['minore'];
        ELSIF NOT ('minore' = ANY(NEW.tags)) THEN
            NEW.tags = array_append(NEW.tags, 'minore');
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

-- === ROLLBACK ===
-- Restores the body that was live before 377 (read from prod on 2026-11-22), which appends a
-- new line on every update.
CREATE OR REPLACE FUNCTION public.check_minor_client()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.date_of_birth IS NOT NULL AND
       DATE_PART('year', AGE(NEW.date_of_birth)) < 18 THEN
        NEW.notes = COALESCE(NEW.notes || E'\n', '') ||
            '[MINORE] Verificare collegamento a profilo genitore. Età: ' ||
            DATE_PART('year', AGE(NEW.date_of_birth))::text || ' anni.';
        IF NEW.tags IS NULL THEN
            NEW.tags = ARRAY['minore'];
        ELSIF NOT ('minore' = ANY(NEW.tags)) THEN
            NEW.tags = array_append(NEW.tags, 'minore');
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
