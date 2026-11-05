-- 346_partner_email_outbox_adoption.sql
-- Canonical SQL v2 adoption of historical Python migration 120.
-- Adopts the partner email outbox and its retry/idempotency contract.
-- depends: 345
-- The forward DDL below is mechanically extracted from the historical apply()
-- statements; extracted forward SHA-256: e89177c4fd0ed09c1ec7f0156c31d614e5a8b383ef94129918be31593c2d2fa6.
-- Existing extra columns/constraints/indexes are allowed. Missing or changed
-- historical objects fail closed through the catalog signature guard.

DO $migration_346_prerequisites$
DECLARE
    missing_relations text[];
BEGIN
    SELECT array_agg(name ORDER BY name)
      INTO missing_relations
      FROM unnest(ARRAY[
        'partners',
        'partner_commissions',
        'team_members'
    ]::text[]) AS required(name)
     WHERE to_regclass('public.' || name) IS NULL;

    IF missing_relations IS NOT NULL THEN
        RAISE EXCEPTION
            'Migration 346 refused: prerequisite relations missing: %', missing_relations
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_346_prerequisites$;

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'partner_email_outbox'
            ) THEN
                CREATE TABLE partner_email_outbox (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                    -- What to send
                    email_type TEXT NOT NULL
                        CHECK (email_type IN ('welcome', 'commission_earned')),
                    partner_id UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
                    commission_id UUID REFERENCES partner_commissions(id) ON DELETE RESTRICT,
                    -- commission_id NULL for welcome emails; set for commission_earned

                    -- Rendered payload (prepared at enqueue time to freeze the content)
                    to_email TEXT NOT NULL,
                    cc_emails TEXT[] DEFAULT '{}',
                    subject TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,

                    -- Lifecycle
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed_dlq')),
                    attempts INT NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    sent_at TIMESTAMPTZ,

                    -- Audit
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- CATA-5: created_by references team_members(id VARCHAR), not users(id UUID)
                    created_by VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,

                    -- Idempotency: one outbox row per (type, partner_id, commission_id)
                    -- Prevents double-enqueue if the mutation is retried.
                    idempotency_key TEXT UNIQUE
                );
            END IF;
        END $$;

CREATE INDEX IF NOT EXISTS idx_partner_email_outbox_pending ON partner_email_outbox (status, next_retry_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_partner_email_outbox_partner_id ON partner_email_outbox (partner_id);

DO $migration_346_catalog_guard$
DECLARE
    expected_columns text[] := ARRAY[
        'partner_email_outbox.attempts',
        'partner_email_outbox.body_markdown',
        'partner_email_outbox.cc_emails',
        'partner_email_outbox.commission_id',
        'partner_email_outbox.created_at',
        'partner_email_outbox.created_by',
        'partner_email_outbox.email_type',
        'partner_email_outbox.id',
        'partner_email_outbox.idempotency_key',
        'partner_email_outbox.last_error',
        'partner_email_outbox.next_retry_at',
        'partner_email_outbox.partner_id',
        'partner_email_outbox.sent_at',
        'partner_email_outbox.status',
        'partner_email_outbox.subject',
        'partner_email_outbox.to_email'
    ]::text[];
    expected_constraints text[] := ARRAY[
        'partner_email_outbox.partner_email_outbox_commission_id_fkey',
        'partner_email_outbox.partner_email_outbox_created_by_fkey',
        'partner_email_outbox.partner_email_outbox_email_type_check',
        'partner_email_outbox.partner_email_outbox_idempotency_key_key',
        'partner_email_outbox.partner_email_outbox_partner_id_fkey',
        'partner_email_outbox.partner_email_outbox_pkey',
        'partner_email_outbox.partner_email_outbox_status_check'
    ]::text[];
    expected_indexes text[] := ARRAY[
        'partner_email_outbox.idx_partner_email_outbox_partner_id',
        'partner_email_outbox.idx_partner_email_outbox_pending'
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
    IF actual_count <> 16 OR actual_hash IS DISTINCT FROM concat(
        'c353', '55d5', 'c21e', '1cd2', 'ef42', '393e', '8492', '31b7'
    ) THEN
        RAISE EXCEPTION
            'Migration 346 refused: column catalog drift (count %, signature %)',
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
    IF actual_count <> 7 OR actual_hash IS DISTINCT FROM concat(
        '85b7', '0b92', '49d0', '057a', 'fd26', '718b', '63e2', 'ecdd'
    ) THEN
        RAISE EXCEPTION
            'Migration 346 refused: constraint catalog drift (count %, signature %)',
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
    IF actual_count <> 2 OR actual_hash IS DISTINCT FROM concat(
        '9e9b', '495b', '5eff', '919f', '4a46', 'ec53', '76da', '91e9'
    ) THEN
        RAISE EXCEPTION
            'Migration 346 refused: index catalog drift (count %, signature %)',
            actual_count, actual_hash
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_346_catalog_guard$;

-- === ROLLBACK ===

-- This adoption may register objects created by the historical Python executor.
-- Dropping them would destroy data the numbered migration did not necessarily create.
-- Refuse and require an explicit compensating migration instead.
DO $migration_346_rollback_refusal$
BEGIN
    RAISE EXCEPTION
        'Migration 346 (partner email outbox) is an adoption migration and cannot be rolled back destructively; use an explicit compensating migration.';
END;
$migration_346_rollback_refusal$;
