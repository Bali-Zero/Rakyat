-- 345_partners_module_adoption.sql
-- Canonical SQL v2 adoption of historical Python migration 119.
-- Adopts partners, referrals, commissions, audit log, team linkage and settings.
-- depends: 344
-- The forward DDL below is mechanically extracted from the historical apply()
-- statements; extracted forward SHA-256: 5ee1a0d945943f34e94acef97d803118a2c764bfd9e71e457fab64605a100136.
-- Existing extra columns/constraints/indexes are allowed. Missing or changed
-- historical objects fail closed through the catalog signature guard.

DO $migration_345_prerequisites$
DECLARE
    missing_relations text[];
BEGIN
    SELECT array_agg(name ORDER BY name)
      INTO missing_relations
      FROM unnest(ARRAY[
        'team_members',
        'practices',
        'system_settings'
    ]::text[]) AS required(name)
     WHERE to_regclass('public.' || name) IS NULL;

    IF missing_relations IS NOT NULL THEN
        RAISE EXCEPTION
            'Migration 345 refused: prerequisite relations missing: %', missing_relations
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_345_prerequisites$;

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'partners'
            ) THEN
                CREATE TABLE partners (
                    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                    -- anagrafica
                    full_name                 TEXT NOT NULL,
                    work_role                 TEXT,
                    company_name              TEXT,
                    office_address            TEXT,
                    email                     TEXT NOT NULL UNIQUE,
                    phone                     TEXT,
                    preferred_language        TEXT DEFAULT 'id',

                    -- fiscal (Indonesia)
                    entity_type               TEXT NOT NULL
                        CHECK (entity_type IN ('individual','corporate_pt','corporate_cv','foreign')),
                    npwp                      TEXT,
                    nik                       TEXT,
                    tax_withholding_category  TEXT NOT NULL DEFAULT 'tbd'
                        CHECK (tax_withholding_category IN ('pph21','pph23','exempt','tbd')),
                    fiscal_address            TEXT,

                    -- payment rail
                    bank_name                 TEXT,
                    bank_account_holder       TEXT,
                    bank_account_number       TEXT,
                    ewallet_type              TEXT,
                    ewallet_number            TEXT,
                    payment_currency          TEXT NOT NULL DEFAULT 'IDR',
                    iban                      TEXT,
                    payment_notes             TEXT,

                    -- commission policy (v1 uses partner-level defaults)
                    default_commission_type   TEXT NOT NULL DEFAULT 'percentage'
                        CHECK (default_commission_type IN ('percentage','flat')),
                    default_commission_value  NUMERIC(14,4) NOT NULL DEFAULT 10.0,

                    -- lifecycle
                    onboarding_status         TEXT NOT NULL DEFAULT 'pending_approval'
                        CHECK (onboarding_status IN ('pending_approval','active','inactive')),
                    -- CATA-5: assigned_to references team_members(id VARCHAR), not users(id UUID)
                    assigned_to               VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,

                    -- UU PDP + T&C
                    pdp_consent_at            TIMESTAMPTZ,
                    pdp_consent_version       TEXT,
                    terms_accepted_at         TIMESTAMPTZ,
                    terms_version             TEXT,

                    -- audit + idempotency
                    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- CATA-5: created_by references team_members(id VARCHAR), not users(id UUID)
                    created_by                VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,
                    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
                    deactivated_at            TIMESTAMPTZ,
                    welcome_email_sent_at     TIMESTAMPTZ
                );
            END IF;
        END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_partners_email ON partners (email);

CREATE INDEX IF NOT EXISTS idx_partners_assigned_to ON partners (assigned_to) WHERE assigned_to IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_partners_onboarding_status ON partners (onboarding_status);

CREATE INDEX IF NOT EXISTS idx_partners_entity_type ON partners (entity_type);

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'team_members' AND column_name = 'partner_id'
            ) THEN
                ALTER TABLE team_members ADD COLUMN partner_id UUID REFERENCES partners(id) ON DELETE SET NULL;
            END IF;
        END $$;

CREATE INDEX IF NOT EXISTS idx_team_members_partner_id ON team_members (partner_id) WHERE partner_id IS NOT NULL;

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'partner_referrals'
            ) THEN
                CREATE TABLE partner_referrals (
                    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    partner_id           UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
                    practice_id          INTEGER NOT NULL REFERENCES practices(id) ON DELETE RESTRICT,
                    share_percent        NUMERIC(5,2) NOT NULL DEFAULT 100.00
                        CHECK (share_percent > 0 AND share_percent <= 100),
                    referred_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- CATA-5: referred_by_user_id references team_members(id VARCHAR)
                    referred_by_user_id  VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,
                    notes                TEXT,

                    CONSTRAINT partner_referrals_practice_unique_v1 UNIQUE (practice_id)
                    -- v2: drop this constraint when enabling split commissions
                );
            END IF;
        END $$;

CREATE INDEX IF NOT EXISTS idx_partner_referrals_partner_id ON partner_referrals (partner_id);

CREATE INDEX IF NOT EXISTS idx_partner_referrals_practice_id ON partner_referrals (practice_id);

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'partner_commissions'
            ) THEN
                CREATE TABLE partner_commissions (
                    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    partner_id               UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
                    referral_id              UUID REFERENCES partner_referrals(id) ON DELETE RESTRICT,
                    practice_id              INTEGER REFERENCES practices(id) ON DELETE RESTRICT,

                    -- row type
                    entry_type               TEXT NOT NULL
                        CHECK (entry_type IN ('accrual','clawback','manual_adjustment')),
                    related_commission_id    UUID REFERENCES partner_commissions(id) ON DELETE RESTRICT,

                    -- immutable snapshot
                    base_amount_idr          NUMERIC(16,2) NOT NULL,
                    commission_type_snapshot TEXT NOT NULL
                        CHECK (commission_type_snapshot IN ('percentage','flat')),
                    commission_value_snapshot NUMERIC(14,4) NOT NULL,
                    rule_source              TEXT NOT NULL DEFAULT 'partner_default'
                        CHECK (rule_source IN ('partner_default','manual_override')),
                    -- CATA-5: assigned_to_snapshot references team_members(id VARCHAR)
                    assigned_to_snapshot     VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,

                    -- amounts (IDR; clawbacks store NEGATIVE)
                    gross_amount_idr         NUMERIC(16,2) NOT NULL,
                    withholding_category     TEXT NOT NULL DEFAULT 'tbd'
                        CHECK (withholding_category IN ('pph21','pph23','exempt','tbd')),
                    withholding_rate         NUMERIC(6,4) NOT NULL DEFAULT 0.0,
                    withholding_amount_idr   NUMERIC(16,2) NOT NULL DEFAULT 0.0,
                    net_amount_idr           NUMERIC(16,2) NOT NULL,

                    -- status lifecycle
                    status                   TEXT NOT NULL DEFAULT 'accrued'
                        CHECK (status IN (
                            'accrued','approved','paid',
                            'clawback_pending','offset_applied',
                            'waived','repaid'
                        )),
                    accrued_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
                    eligible_for_approval_at TIMESTAMPTZ NOT NULL,
                    approved_at              TIMESTAMPTZ,
                    -- CATA-5: approved_by references team_members(id VARCHAR)
                    approved_by              VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,
                    paid_at                  TIMESTAMPTZ,
                    -- CATA-5: paid_by references team_members(id VARCHAR)
                    paid_by                  VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,
                    paid_via                 TEXT,
                    payment_reference        TEXT,
                    payment_proof_url        TEXT,
                    receipt_type             TEXT
                        CHECK (receipt_type IS NULL OR receipt_type IN ('kwitansi','invoice','none')),
                    receipt_file_url         TEXT,

                    -- finance / clawback audit
                    manual_override_reason   TEXT,
                    clawback_reason          TEXT,
                    waiver_reason            TEXT,
                    idempotency_key          TEXT UNIQUE,
                    commission_email_sent_at TIMESTAMPTZ,

                    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            END IF;
        END $$;

CREATE INDEX IF NOT EXISTS idx_partner_commissions_partner_id ON partner_commissions (partner_id);

CREATE INDEX IF NOT EXISTS idx_partner_commissions_practice_id ON partner_commissions (practice_id);

CREATE INDEX IF NOT EXISTS idx_partner_commissions_status ON partner_commissions (status);

CREATE INDEX IF NOT EXISTS idx_partner_commissions_eligible_at ON partner_commissions (eligible_for_approval_at) WHERE status = 'accrued';

CREATE INDEX IF NOT EXISTS idx_partner_commissions_assigned_to_snapshot ON partner_commissions (assigned_to_snapshot) WHERE assigned_to_snapshot IS NOT NULL;

DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = 'partner_audit_log'
            ) THEN
                CREATE TABLE partner_audit_log (
                    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    partner_id    UUID NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
                    -- CATA-5: actor_user_id references team_members(id VARCHAR)
                    actor_user_id VARCHAR REFERENCES team_members(id) ON DELETE SET NULL,
                    action        TEXT NOT NULL,
                    before_json   JSONB,
                    after_json    JSONB,
                    reason        TEXT,
                    at            TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            END IF;
        END $$;

CREATE INDEX IF NOT EXISTS idx_partner_audit_log_partner_id ON partner_audit_log (partner_id);

CREATE INDEX IF NOT EXISTS idx_partner_audit_log_at ON partner_audit_log (at DESC);

INSERT INTO system_settings (key, value) VALUES
          ('partner_clawback_auto_writeoff_idr', '0'),
          ('partner_accrual_cooling_off_days', '30'),
          ('partner_withholding_rate_pph21', '2.5'),
          ('partner_withholding_rate_pph23', '2.0'),
          ('partner_withholding_no_npwp_surcharge', '20')
        ON CONFLICT (key) DO NOTHING;

DO $migration_345_catalog_guard$
DECLARE
    expected_columns text[] := ARRAY[
        'partner_audit_log.action',
        'partner_audit_log.actor_user_id',
        'partner_audit_log.after_json',
        'partner_audit_log.at',
        'partner_audit_log.before_json',
        'partner_audit_log.id',
        'partner_audit_log.partner_id',
        'partner_audit_log.reason',
        'partner_commissions.accrued_at',
        'partner_commissions.approved_at',
        'partner_commissions.approved_by',
        'partner_commissions.assigned_to_snapshot',
        'partner_commissions.base_amount_idr',
        'partner_commissions.clawback_reason',
        'partner_commissions.commission_email_sent_at',
        'partner_commissions.commission_type_snapshot',
        'partner_commissions.commission_value_snapshot',
        'partner_commissions.created_at',
        'partner_commissions.eligible_for_approval_at',
        'partner_commissions.entry_type',
        'partner_commissions.gross_amount_idr',
        'partner_commissions.id',
        'partner_commissions.idempotency_key',
        'partner_commissions.manual_override_reason',
        'partner_commissions.net_amount_idr',
        'partner_commissions.paid_at',
        'partner_commissions.paid_by',
        'partner_commissions.paid_via',
        'partner_commissions.partner_id',
        'partner_commissions.payment_proof_url',
        'partner_commissions.payment_reference',
        'partner_commissions.practice_id',
        'partner_commissions.receipt_file_url',
        'partner_commissions.receipt_type',
        'partner_commissions.referral_id',
        'partner_commissions.related_commission_id',
        'partner_commissions.rule_source',
        'partner_commissions.status',
        'partner_commissions.waiver_reason',
        'partner_commissions.withholding_amount_idr',
        'partner_commissions.withholding_category',
        'partner_commissions.withholding_rate',
        'partner_referrals.id',
        'partner_referrals.notes',
        'partner_referrals.partner_id',
        'partner_referrals.practice_id',
        'partner_referrals.referred_at',
        'partner_referrals.referred_by_user_id',
        'partner_referrals.share_percent',
        'partners.assigned_to',
        'partners.bank_account_holder',
        'partners.bank_account_number',
        'partners.bank_name',
        'partners.company_name',
        'partners.created_at',
        'partners.created_by',
        'partners.deactivated_at',
        'partners.default_commission_type',
        'partners.default_commission_value',
        'partners.email',
        'partners.entity_type',
        'partners.ewallet_number',
        'partners.ewallet_type',
        'partners.fiscal_address',
        'partners.full_name',
        'partners.iban',
        'partners.id',
        'partners.nik',
        'partners.npwp',
        'partners.office_address',
        'partners.onboarding_status',
        'partners.payment_currency',
        'partners.payment_notes',
        'partners.pdp_consent_at',
        'partners.pdp_consent_version',
        'partners.phone',
        'partners.preferred_language',
        'partners.tax_withholding_category',
        'partners.terms_accepted_at',
        'partners.terms_version',
        'partners.updated_at',
        'partners.welcome_email_sent_at',
        'partners.work_role',
        'team_members.partner_id'
    ]::text[];
    expected_constraints text[] := ARRAY[
        'partner_audit_log.partner_audit_log_actor_user_id_fkey',
        'partner_audit_log.partner_audit_log_partner_id_fkey',
        'partner_audit_log.partner_audit_log_pkey',
        'partner_commissions.partner_commissions_approved_by_fkey',
        'partner_commissions.partner_commissions_assigned_to_snapshot_fkey',
        'partner_commissions.partner_commissions_commission_type_snapshot_check',
        'partner_commissions.partner_commissions_entry_type_check',
        'partner_commissions.partner_commissions_idempotency_key_key',
        'partner_commissions.partner_commissions_paid_by_fkey',
        'partner_commissions.partner_commissions_partner_id_fkey',
        'partner_commissions.partner_commissions_pkey',
        'partner_commissions.partner_commissions_practice_id_fkey',
        'partner_commissions.partner_commissions_receipt_type_check',
        'partner_commissions.partner_commissions_referral_id_fkey',
        'partner_commissions.partner_commissions_related_commission_id_fkey',
        'partner_commissions.partner_commissions_rule_source_check',
        'partner_commissions.partner_commissions_status_check',
        'partner_commissions.partner_commissions_withholding_category_check',
        'partner_referrals.partner_referrals_partner_id_fkey',
        'partner_referrals.partner_referrals_pkey',
        'partner_referrals.partner_referrals_practice_id_fkey',
        'partner_referrals.partner_referrals_practice_unique_v1',
        'partner_referrals.partner_referrals_referred_by_user_id_fkey',
        'partner_referrals.partner_referrals_share_percent_check',
        'partners.partners_assigned_to_fkey',
        'partners.partners_created_by_fkey',
        'partners.partners_default_commission_type_check',
        'partners.partners_email_key',
        'partners.partners_entity_type_check',
        'partners.partners_onboarding_status_check',
        'partners.partners_pkey',
        'partners.partners_tax_withholding_category_check',
        'team_members.team_members_partner_id_fkey'
    ]::text[];
    expected_indexes text[] := ARRAY[
        'partner_audit_log.idx_partner_audit_log_at',
        'partner_audit_log.idx_partner_audit_log_partner_id',
        'partner_commissions.idx_partner_commissions_assigned_to_snapshot',
        'partner_commissions.idx_partner_commissions_eligible_at',
        'partner_commissions.idx_partner_commissions_partner_id',
        'partner_commissions.idx_partner_commissions_practice_id',
        'partner_commissions.idx_partner_commissions_status',
        'partner_referrals.idx_partner_referrals_partner_id',
        'partner_referrals.idx_partner_referrals_practice_id',
        'partners.idx_partners_assigned_to',
        'partners.idx_partners_email',
        'partners.idx_partners_entity_type',
        'partners.idx_partners_onboarding_status',
        'team_members.idx_team_members_partner_id'
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
    IF actual_count <> 84 OR actual_hash IS DISTINCT FROM concat(
        '910f', '0eaa', '6b8b', '57c7', '2c1f', '5469', 'f883', '88b1'
    ) THEN
        RAISE EXCEPTION
            'Migration 345 refused: column catalog drift (count %, signature %)',
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
    IF actual_count <> 33 OR actual_hash IS DISTINCT FROM concat(
        'd6a2', '91b5', 'b497', 'cfee', '8cdf', '1a5d', 'ef9f', 'a9da'
    ) THEN
        RAISE EXCEPTION
            'Migration 345 refused: constraint catalog drift (count %, signature %)',
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
    IF actual_count <> 14 OR actual_hash IS DISTINCT FROM concat(
        '691d', '606d', '484a', 'afae', '6b1c', '4b2f', '96e3', '41cb'
    ) THEN
        RAISE EXCEPTION
            'Migration 345 refused: index catalog drift (count %, signature %)',
            actual_count, actual_hash
            USING ERRCODE = 'check_violation';
    END IF;
END;
$migration_345_catalog_guard$;

-- === ROLLBACK ===

-- This adoption may register objects created by the historical Python executor.
-- Dropping them would destroy data the numbered migration did not necessarily create.
-- Refuse and require an explicit compensating migration instead.
DO $migration_345_rollback_refusal$
BEGIN
    RAISE EXCEPTION
        'Migration 345 (partners module) is an adoption migration and cannot be rolled back destructively; use an explicit compensating migration.';
END;
$migration_345_rollback_refusal$;
