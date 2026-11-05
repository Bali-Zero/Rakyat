-- ============================================================
-- 349_client_ai_recaps.sql
-- Cache of the short AI recap shown on the kita client card
-- Date: 2026-11-03
--
-- Owner decision 2026-11-03 (hybrid, 2026-11-04): an automatic 2-3 sentence
-- English recap next to the current visa title (Immigration tab) and next to
-- the company name (Company tab). Built from an allow-listed, PII-free fact
-- set (backend/services/crm/client_recap.py): a deterministic template is
-- stored at once (model = 'template'); an optional provider rewrite replaces
-- it only after validation (model = '<provider>:<model>'). The table stores
-- the current text per (client, scope, subject) with the sha256 of the
-- versioned canonical fact JSON. A request recomputes the facts and
-- regenerates only when the hash changed, which is what makes the recap
-- refresh by itself when the underlying data changes.
--
-- scope        'immigration' | 'company'
-- subject_key  '' for immigration, the company id (as text) for company
-- text         the template or the validated rewrite; NULL is tolerated and treated as absent
--
-- Brand-new table: no client column is referenced (no FK on purpose, same
-- as the other derived caches; a deleted client leaves an inert row).
--
-- Must be kept in sync with:
--   - backend/services/crm/client_recap.py (SQL constants)
-- ============================================================

CREATE TABLE IF NOT EXISTS client_ai_recaps (
    client_id    INT          NOT NULL,
    scope        TEXT         NOT NULL,
    subject_key  TEXT         NOT NULL DEFAULT '',
    facts_hash   TEXT         NOT NULL,
    text         TEXT,
    model        TEXT,
    generated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, scope, subject_key),
    CONSTRAINT client_ai_recaps_scope_check CHECK (scope IN ('immigration', 'company'))
);

-- === ROLLBACK ===
-- DROP TABLE IF EXISTS client_ai_recaps;
