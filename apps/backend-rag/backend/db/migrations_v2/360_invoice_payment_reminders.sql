-- ============================================================
-- 360_invoice_payment_reminders.sql
-- One row per invoice that accounting has already been reminded about,
-- so the unpaid-invoice digest names each invoice ONCE, across reruns,
-- restarts and overlapping runs (PRIMARY KEY on invoice_id).
-- Date: 2026-11-13
--
-- Owner decision 2026-11-13: remind Asya (accounting) when an invoice is
-- still unpaid 3 days after issue; one reminder per invoice, no daily
-- repeats; only invoices issued from 2026-11-11 (WITA), the backlog is
-- never emailed.
--
-- No foreign key to invoices on purpose: CI builds a reduced schema where
-- invoices may be absent (see 355), and a reminder row must survive an
-- invoice regeneration. invoice_id is the invoices.id the digest named.
--
-- Consumer: services/invoicing/unpaid_invoice_notifier.py, fired daily by
-- POST /api/cron/notifiers/all (Mini launchd com.balizero.zero-cron.
-- notifiers-all, 00:00 WITA).
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS invoice_payment_reminders (
    invoice_id  INTEGER PRIMARY KEY,
    reminded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    days_unpaid INTEGER NOT NULL
);

COMMENT ON TABLE invoice_payment_reminders IS
    'Invoices already named in an unpaid-invoice digest to accounting. Insert-only: a row means "reminded once", never deleted by the notifier.';

-- === ROLLBACK ===

DROP TABLE IF EXISTS invoice_payment_reminders;
