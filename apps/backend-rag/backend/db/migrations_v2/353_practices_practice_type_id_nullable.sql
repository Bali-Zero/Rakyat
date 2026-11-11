-- 353_practices_practice_type_id_nullable.sql
--
-- Promote the last bootstrap-only `DROP NOT NULL` into migrations_v2.
-- Port of Teman2 PR #7938 (migration 324 there, never landed: 324/325 are
-- absent from the production ledger, which jumps 323 -> 326 -> 352).
--
-- `ci_bootstrap_schema.py` issues
--   ALTER TABLE practices ALTER COLUMN practice_type_id DROP NOT NULL
-- claiming it mirrors prod, and until this file nothing corroborated that
-- claim — of the three `DROP NOT NULL` families in the bootstrap
-- (lkpm_reports.realized_*/cumulative_*, team_members.full_name,
-- practices.practice_type_id) the first two each have a committed
-- migration counterpart (132, 137) and this one had none.
--
-- Settled against the production leader (read-only,
-- information_schema.columns): is_nullable = 'YES' — measured 2026-10-05
-- for #7938 and re-measured 2026-11-11 (`nuzantara_readonly@nuzantara_rag`:
-- practices.practice_type_id is_nullable = YES). Prod genuinely enforces
-- no NOT NULL on this column. This file records that fact.
--
-- Nullability is NOT where the product guarantees a type. The CRM read
-- paths INNER JOIN practice_types, so a practice with a NULL type is
-- invisible in the product; the write path resolves code->id and INSERTs
-- the column unconditionally, and migration 311 routes no-type inquiries
-- through a placeholder practice_types row precisely because a NULL type
-- would hide them.
--
-- The SQLModel CRM module (backend/app/modules/crm/models.py) already
-- mirrors prod (nullable, measured for #7938 on 2026-10-06).
--
-- Idempotent. On prod (already nullable) and on bootstrap-built CI (the
-- bootstrap script issues the same statement before the v2 runner applies
-- this migration, which then records itself as applied) the statement
-- changes no constraint — though it still takes ACCESS EXCLUSIVE on the
-- table for the catalog rewrite, same as any ALTER COLUMN.

ALTER TABLE practices ALTER COLUMN practice_type_id DROP NOT NULL;

-- === ROLLBACK ===
-- This is an ADOPTION migration: the forward records a measured prod
-- fact and changes no constraint anywhere it runs (prod and CI are
-- already nullable; the SQLModel create_all shape is nullable too).
-- There is therefore no prior state to restore, and a rollback that
-- SET NOT NULL would INVENT a constraint the forward never removed —
-- with zero NULL rows it would even succeed silently.
-- The only honest rollback is a REFUSAL: raising keeps both ledgers
-- intact (migration_manager deletes them only after the rollback SQL
-- succeeds), so a refused rollback can never masquerade as a removal.
DO $$
BEGIN
  RAISE EXCEPTION 'rollback 353 refused: adoption migration — forward was a measured no-op (practices.practice_type_id already nullable on prod, CI and create_all); nothing to restore';
END $$;
