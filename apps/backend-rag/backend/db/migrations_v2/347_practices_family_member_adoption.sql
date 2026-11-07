-- 347_practices_family_member_adoption.sql
-- Canonical SQL v2 adoption of historical Python migration 121.
-- Adopts practices.family_member_id, its foreign key and lookup index.
-- depends: 346
-- The forward DDL below is mechanically extracted from the historical apply()
-- statements; extracted forward SHA-256: d163991a92404614c523a287c2f0573ecc010b599dc138cffe6729dc575fdc49.
-- Existing extra columns/constraints/indexes are allowed. Missing or changed
-- historical objects fail closed through the catalog signature guard.

DO $migration_347_prerequisites$
DECLARE
    missing_relations text[];
BEGIN
    SELECT array_agg(name ORDER BY name)
      INTO missing_relations
      FROM unnest(ARRAY[
        'practices',
        'client_family_members'
    ]::text[]) AS required(name)
     WHERE to_regclass('public.' || name) IS NULL;

    IF missing_relations IS NOT NULL THEN
        RAISE EXCEPTION
            'Migration 347 refused: prerequisite relations missing: %', missing_relations
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_347_prerequisites$;

-- This intentional first-time nullable FK can scan and takes relation locks.
-- Existing legacy-121 schemas skip ADD COLUMN, then face the same strict guard.
-- Keep the historical inline DDL: splitting ADD FK would heal existing drift.
-- Bound each statement on the actual DDL connection, independently of the pool.
-- The executor also sets a 2s lock timeout per attempt; 57014 is not retried.
SET LOCAL statement_timeout = '60s';

ALTER TABLE practices
            ADD COLUMN IF NOT EXISTS family_member_id BIGINT
                REFERENCES client_family_members(id) ON DELETE SET NULL; -- squawk-ignore adding-foreign-key-constraint

CREATE INDEX IF NOT EXISTS idx_practices_family_member_id ON practices (family_member_id) WHERE family_member_id IS NOT NULL;

DO $migration_347_catalog_guard$
DECLARE
    expected_columns text[] := ARRAY[
        'practices.family_member_id'
    ]::text[];
    expected_constraints text[] := ARRAY[
        'practices.practices_family_member_id_fkey'
    ]::text[];
    expected_indexes text[] := ARRAY[
        'practices.idx_practices_family_member_id'
    ]::text[];
    actual_count bigint;
    actual_hash text;
BEGIN
    SELECT count(*), md5(string_agg(signature, E'\n' ORDER BY object_name))
      INTO actual_count, actual_hash
      FROM (
        SELECT c.relname || '.' || a.attname AS object_name,
               c.relname || '|' || a.attname || '|' ||
               format_type(a.atttypid, a.atttypmod) || '|' ||
               a.attnotnull::text || '|' ||
               COALESCE(pg_get_expr(d.adbin, d.adrelid), '') AS signature
          FROM pg_class c
          JOIN pg_namespace nsp ON nsp.oid = c.relnamespace AND nsp.nspname = 'public'
          JOIN pg_attribute a ON a.attrelid = c.oid
                             AND a.attnum > 0
                             AND NOT a.attisdropped
          LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
         WHERE c.relname || '.' || a.attname = ANY(expected_columns)
      ) AS observed;
    IF actual_count <> 1 OR actual_hash IS DISTINCT FROM concat(
        '6696', 'eeb8', 'a51e', 'f6a9', 'e56b', '7c53', 'a454', 'e2bc'
    ) THEN
        RAISE EXCEPTION
            'Migration 347 refused: column catalog drift (count %, signature %)',
            actual_count, actual_hash
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT count(*), md5(string_agg(signature, E'\n' ORDER BY object_name))
      INTO actual_count, actual_hash
      FROM (
        SELECT c.relname || '.' || con.conname AS object_name,
               c.relname || '|' || con.conname || '|' || con.contype::text || '|' ||
               pg_get_constraintdef(con.oid, true) AS signature
          FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace nsp ON nsp.oid = c.relnamespace AND nsp.nspname = 'public'
         WHERE c.relname || '.' || con.conname = ANY(expected_constraints)
      ) AS observed;
    IF actual_count <> 1 OR actual_hash IS DISTINCT FROM concat(
        '34db', 'f3fd', 'bbed', '9433', 'd7ed', 'e6fb', '20c9', 'c665'
    ) THEN
        RAISE EXCEPTION
            'Migration 347 refused: constraint catalog drift (count %, signature %)',
            actual_count, actual_hash
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT count(*), md5(string_agg(signature, E'\n' ORDER BY object_name))
      INTO actual_count, actual_hash
      FROM (
        SELECT tablename || '.' || indexname AS object_name,
               tablename || '|' || indexname || '|' || indexdef AS signature
          FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename || '.' || indexname = ANY(expected_indexes)
      ) AS observed;
    IF actual_count <> 1 OR actual_hash IS DISTINCT FROM concat(
        '349a', '84ba', 'e5c3', '37c3', '8944', 'ede3', 'a118', '465c'
    ) THEN
        RAISE EXCEPTION
            'Migration 347 refused: index catalog drift (count %, signature %)',
            actual_count, actual_hash
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_347_catalog_guard$;

-- === ROLLBACK ===

-- This adoption may register objects created by the historical Python executor.
-- Dropping them would destroy data the numbered migration did not necessarily create.
-- Refuse and require an explicit compensating migration instead.
DO $migration_347_rollback_refusal$
BEGIN
    RAISE EXCEPTION
        'Migration 347 (practices family member) is an adoption migration and cannot be rolled back destructively; use an explicit compensating migration.';
END;
$migration_347_rollback_refusal$;
