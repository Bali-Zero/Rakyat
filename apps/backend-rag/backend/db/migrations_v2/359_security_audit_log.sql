-- Migration 359: create security_audit_log in the v2 system (promoted from legacy migration_090).
--
-- security_audit_log was defined only by backend/migrations/migration_090_security_audit_log.py,
-- which the v2 runner never discovers. Measured 2026-11-13 on prod:
-- to_regclass('public.security_audit_log') IS NULL. Its writers (backend.services.security.
-- audit_service.SecurityAuditService.log_event, reached from the portal impersonation audit and
-- the auth revoke-all route) are best-effort and swallow the error, so every audit row has been
-- lost silently. Owner approved creating the table on 2026-11-13.
--
-- The DDL mirrors migration_090 exactly (columns, types, defaults, the three indexes) and also
-- scripts/qa/synthetic_gate_schema.sql. Idempotent (IF NOT EXISTS) and forward-only: no data, no
-- DROP, no GRANT. The table holds user emails and IPs, so it deliberately gets no grant beyond
-- whatever the database's default privileges already give.
--
-- Migration 090 also wrote a row into legacy migration_history; v2 does not read that ledger,
-- so this file records itself in _schema_versions only.
-- depends: 358
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS public.security_audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255),
    user_email VARCHAR(255),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100),
    resource_id VARCHAR(255),
    ip_address INET,
    user_agent TEXT,
    details JSONB,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_audit_user
    ON public.security_audit_log(user_email, created_at);
CREATE INDEX IF NOT EXISTS idx_security_audit_action
    ON public.security_audit_log(action, created_at);
CREATE INDEX IF NOT EXISTS idx_security_audit_created
    ON public.security_audit_log(created_at);

-- === ROLLBACK ===
-- Intentionally no-op, like 107. The table is a security audit trail: a generic CLI rollback
-- must not destroy it. Any rollback must be a separate, named migration with explicit operator
-- review.
SELECT 1;
