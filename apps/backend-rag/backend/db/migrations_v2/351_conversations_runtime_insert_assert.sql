-- 351_conversations_runtime_insert_assert.sql
-- Asserts, at apply time only, that the runtime role holds what chat persistence
-- needs on public.conversations. A migration runs once: the every-release
-- assertion is the conversations_runtime_privileges check in
-- backend/db/schema_audit.py, run by release_command on each deploy.
--
-- Why: backend_rag_v2 relied on the superuser bypass for INSERT on conversations.
-- The W38 NOSUPERUSER demotion removed the bypass without any error, and no
-- migration granted or asserted the privilege, so chat persistence failed quietly:
-- measured absent on 2026-08-08 and again on 2026-11-09, until the owner granted
-- INSERT by hand that day. This migration makes a rebuilt environment fail loudly at apply
-- time and, on a database applied by the table owner, grants the privileges.
--
-- Asserted for backend_rag_v2: SELECT, INSERT, UPDATE on public.conversations
-- and USAGE on the sequence behind conversations.id.
--
-- Who runs what:
-- - In production release_command runs as session_user backend_rag_migrator with
--   current_user backend_rag_v2 (_schema_versions.applied_as), a non-owner: the
--   table is owned by zantara_rag_user. It cannot grant, so the GRANT attempt
--   below either warns or is a no-op, and the checks verify and raise with the
--   exact manual statement for an owner/admin to run.
-- - On a rebuilt database applied by the table owner, the GRANT attempt
--   succeeds and the checks then pass.
--
-- A superuser role passes every has_*_privilege() call by definition, so the
-- assertion would prove nothing; the migration raises if backend_rag_v2 is
-- superuser.
--
-- DELETE is deliberately never granted here and is not asserted either: it
-- waits on the owner's decision on the semantics of /clear, DELETE /{id} and
-- admin cleanup.
--
-- No-op when the role or the table does not exist (CI and dev databases).

DO $assert_block$
DECLARE
    runtime_role constant text := 'backend_rag_v2';
    object_name constant text := 'public.conversations';
    object_reg regclass;
    sequence_name text;
    sequence_reg regclass;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime_role) THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = runtime_role AND rolsuper
    ) THEN
        RAISE EXCEPTION
            'backend_rag_v2 is a superuser, so privilege checks prove nothing; demote it with an admin role: ALTER ROLE backend_rag_v2 NOSUPERUSER';
    END IF;

    object_reg := to_regclass(object_name);

    IF object_reg IS NULL THEN
        RETURN;
    END IF;

    -- pg_get_serial_sequence() raises, instead of returning NULL, when the
    -- column does not exist (see fix244-seqbug in migration 244), so test the
    -- column first.
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = object_reg AND attname = 'id' AND NOT attisdropped
    ) THEN
        sequence_name := pg_get_serial_sequence(object_name, 'id');
        sequence_reg := to_regclass(sequence_name);
    END IF;

    IF NOT (
        has_table_privilege(runtime_role, object_reg, 'SELECT')
        AND has_table_privilege(runtime_role, object_reg, 'INSERT')
        AND has_table_privilege(runtime_role, object_reg, 'UPDATE')
    ) THEN
        BEGIN
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE ON TABLE %s TO %I',
                object_reg,
                runtime_role
            );
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE WARNING
                    'manual grant required: GRANT SELECT, INSERT, UPDATE ON TABLE % TO %',
                    object_name,
                    runtime_role;
        END;
    END IF;

    IF sequence_reg IS NOT NULL
       AND NOT has_sequence_privilege(runtime_role, sequence_reg, 'USAGE') THEN
        BEGIN
            EXECUTE format(
                'GRANT USAGE ON SEQUENCE %s TO %I',
                sequence_reg,
                runtime_role
            );
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE WARNING
                    'manual grant required: GRANT USAGE ON SEQUENCE % TO %',
                    sequence_name,
                    runtime_role;
        END;
    END IF;

    IF NOT has_table_privilege(runtime_role, object_reg, 'SELECT') THEN
        RAISE EXCEPTION
            'backend_rag_v2 is missing SELECT on %; apply manual grant with an owner/admin role: GRANT SELECT ON TABLE % TO backend_rag_v2',
            object_name,
            object_name;
    END IF;

    IF NOT has_table_privilege(runtime_role, object_reg, 'INSERT') THEN
        RAISE EXCEPTION
            'backend_rag_v2 is missing INSERT on %; apply manual grant with an owner/admin role: GRANT INSERT ON TABLE % TO backend_rag_v2',
            object_name,
            object_name;
    END IF;

    IF NOT has_table_privilege(runtime_role, object_reg, 'UPDATE') THEN
        RAISE EXCEPTION
            'backend_rag_v2 is missing UPDATE on %; apply manual grant with an owner/admin role: GRANT UPDATE ON TABLE % TO backend_rag_v2',
            object_name,
            object_name;
    END IF;

    IF sequence_reg IS NOT NULL
       AND NOT has_sequence_privilege(runtime_role, sequence_reg, 'USAGE') THEN
        RAISE EXCEPTION
            'backend_rag_v2 is missing USAGE on sequence %; apply manual grant with an owner/admin role: GRANT USAGE ON SEQUENCE % TO backend_rag_v2',
            sequence_name,
            sequence_name;
    END IF;
END
$assert_block$;

-- === ROLLBACK ===
-- Nothing to revert: the privileges asserted here were granted by the owner,
-- not created by this migration, so revoking them would break chat persistence.
DO $rollback_note$
BEGIN
    RAISE NOTICE 'migration 351 created no privileges; rollback is a no-op';
END
$rollback_note$;
