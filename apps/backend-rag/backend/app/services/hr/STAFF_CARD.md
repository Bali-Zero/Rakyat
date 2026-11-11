# Staff HR card: contract

Storage: `hr_employees` scalar columns plus `hr_employees.profile` (jsonb object,
Migration 354). Code: `hr_card_models.py`, `hr_service.py`, `routers/hr.py`,
`backend/scripts/import_staff_cards.py`. Every clause has a named test; the index is at the end.

## Scope

In: ONE read route, `GET /api/hr/me/card`, and the import script. HR fills cards through the
import (stdin JSON); a staff member reads their own card through the route.

Explicitly out, on purpose (a previous design with these was gate-blocked three times):
- **HTTP writes of card data.** No PATCH, no POST, no admin card route. The legacy
  `POST /employees` is untouched (its response no longer carries `profile`).
- **Any new route with a path or query parameter.**
- **Deleting profile data.** Nothing here removes a profile key or a docs slot; a merge only
  adds or overwrites. Removal is a separate decision with its own spec.
- **Case-insensitive or normalised identity matching** on the read path (see C1).

## Clauses

**C1 Own card only, keyed by an identity the SQL re-checks.** The route depends on
`backend.app.dependencies.get_current_user`, which is `backend/app/deps/auth.py:44` (NOT
`routers/auth.py`). Behind the production `HybridAuthMiddleware` it returns the context the
middleware built from the SIGNED CLAIMS (`middleware/hybrid_auth.py:742` and `:795`, set on the
request at `:523`): `user_id` is the token's `sub` (`deps/auth.py:66`), `role` is the token's
`role` claim. Neither the middleware nor that dependency re-reads `team_members`: `active` and the
current email are NOT checked there (revocation is a separate Redis check). So the route trusts
nothing from the claim except the id it uses as a key, and the SQL enforces the rest itself:
`get_card_for_member(id)` is `WHERE tm.id = $1 AND COALESCE(tm.role,'') <> 'client' AND
tm.active = true`, i.e. the PRIMARY KEY, the role of the STORED row (never the claim) and the
stored `active`. The route's own `role == client` claim check (403) is only an early refusal;
a client row behind a staff claim, a deactivated member holding a valid token, or an id that
is not a key (an email in the id slot) all get 404. It never looks anyone up by email, so two
rows whose emails differ only by case cannot cross (`team_members.email` is unique
case-sensitively; the primary key is unique absolutely). 404 also when there is no
`hr_employees` row.

**C2 Why the no-value guarantee holds here, and where it does not.** The route has no
caller-supplied input at all (no path, query or body parameter), so there is no value of the
caller's to echo in a 422 or a request path. The card code raises `HRCardError(code)` with a code
from the closed enum `HRCardCode`, never a driver or pydantic message; the route maps it to a
fixed text. The import prints counts and reason codes only. This spec does NOT claim a
framework-level guarantee (422 bodies or access logs on other routes are not card code).
The member's own card is returned in full except HR free-text `notes`, which are not even selected.

**C3 Reads never validate.** The stored data goes through the READ model (plain optional types)
and coercion helpers that return None, never raise and never quote the value. A stored value the
write validators would reject yields a 200 card; a value of the wrong type is shown as null.

**C4 jsonb through the production codec exactly once.** Python dicts are bound to jsonb
parameters; never a pre-serialised string. The import registers the codec on its connection
(`init_asyncpg_connection`); DB-backed tests use the same registration
(`tests/fixtures/prod_shaped_pool.py`).

**C5 Merge semantics** (`HRService.merge_card_on`). Omitted scalar columns and omitted profile
keys are preserved. `profile`, `profile.docs`, `profile.contract` are objects: a stored non-object
is treated as `{}` before merging; an incoming non-object is rejected (`profile_not_object`)
before any write. `docs` and `contract` merge key by key. `RETURNING jsonb_typeof(profile)` is
checked; anything but an object rolls the write back. A missing row is inserted (needs
`hire_date`); if a concurrent writer inserts first, the insert conflicts and falls through to the update.

**C6 Write validators (import only).** `nik` 16 digits after removing spaces, dots, dashes;
`npwp` 15 or 16 digits likewise; BPJS Kesehatan 13 digits; BPJS Ketenagakerjaan / KPJ 11
alphanumerics (separators removed); bank account 6-20 digits after removing spaces and dashes.
Validation runs on the normalised form; the trimmed value is stored as given. `profile`, `docs`,
`contract` must be objects when present: an explicit `null`, `[]`, `false`, `0` or `""` is rejected
as it came, never normalised to `{}`. Unknown keys are rejected; `is_active` is a strict boolean.

**C7 Import is all-or-nothing with full accounting.** Dry-run by default; `--apply` is one
transaction that commits only when every card is applied (`not_found` tolerated with
`--skip-not-found`). Every card ends in exactly one of `applied`, `not_found`, `rejected:<code>`.
A merge that returns no row is `rejected:not_applied` and the batch rolls back. An unknown key on
the OUTER card object is `rejected:unknown_key`, never dropped. Every argument error (unknown flag,
malformed value for a known flag, stray positional, abbreviation) is one FIXED line on stderr and
exit 2 before any database access: argparse's own message, which quotes the argument, is
replaced; `--help` contains no argv. Output is counts and codes only.

**C8 Import identity, locking and order.** A card is resolved by EXACT email on staff rows
(`role <> 'client'`). If the lower-cased email matches more than one staff row the card is
`rejected:ambiguous_email`; the import never picks one. A single row whose stored email differs
from the card's only by case is `not_found`. In `--apply`, before each merge the resolved
`team_members` row is locked `FOR SHARE` with the exact email and staff condition re-checked in
the same statement: a member renamed or turned into a client since the resolve is
`rejected:member_changed` and nothing is written. Cards are written in sorted resolved-member-id
order (document order for the same member), so two batches take member locks in the same order.
The transaction sets a local `lock_timeout` (5 s); a lock timeout or deadlock is
`rejected:lock_timeout` and the batch rolls back. This does not make concurrent batches
serialisable in general; it bounds the wait and makes the order deterministic.

**C9 Legacy routes.** `GET /employees/{id}` drops `notes` for a non-admin self-read; no legacy
employee read or write response carries `profile`.

## Clause-to-test index

| Clause | Tests |
|---|---|
| C1 | `unit/app/routers/test_hr_staff_card.py` (keyed by id, client 403, 404, one GET without parameters); `unit/app/services/hr/test_hr_card_service.py::test_get_card_for_member_is_keyed_by_primary_key_*`; `tests/migrations/test_migration_354_hr_employees_profile.py`: `::test_me_card_returns_the_authenticated_rows_card_never_the_case_twins`, `::test_get_card_for_member_is_primary_key_only_and_staff_only`, `::test_get_card_for_member_requires_an_active_member`, and through the real middleware with signed tokens `::test_real_auth_path_active_staff_gets_own_card`, `::test_real_auth_path_a_deactivated_member_with_a_valid_token_gets_404`, `::test_real_auth_path_a_staff_claim_on_a_client_row_gets_no_card`, `::test_a_client_claim_is_refused_and_a_forged_id_matches_no_row` |
| C2 | `test_a_database_failure_is_a_fixed_500_*`, `test_a_read_failure_is_a_code_*`, `test_merge_card_on_replaces_a_driver_error_*`, `test_a_card_write_failure_carries_no_driver_text`, `test_output_carries_counts_only`, `test_the_surface_added_is_one_get_*` |
| C3 | `test_a_non_object_stored_profile_still_reads`, `test_odd_stored_strings_*`, `test_the_read_model_never_raises_*`, `test_a_stored_string_profile_and_odd_contract_still_read` |
| C4 | `test_two_merges_keep_profile_an_object_under_the_production_codec`, `test_amain_registers_the_production_codec_*` |
| C5 | `test_nested_merge_survives_*`, `test_a_non_object_profile_is_normalised_*`, `test_merge_keeps_omitted_columns_*`, `test_a_non_object_incoming_part_*`, `test_a_profile_that_would_not_be_an_object_*`, `test_a_concurrent_insert_falls_through_to_the_update_on_two_connections` |
| C6 | `unit/app/services/hr/test_hr_card_models.py`, `test_a_present_non_object_*`, `test_a_non_object_docs_or_contract_*` (import) |
| C7 | `test_apply_*`, `test_import_apply_is_atomic_*`, `test_import_second_merge_returning_none_*`, `test_every_card_is_accounted_for_exactly_once`, `test_unknown_flag_exits_2_*`, `test_the_real_entry_point_fails_every_parse_error_with_a_fixed_text` (subprocess), `test_help_output_contains_no_argv`, `test_an_unknown_key_on_the_outer_card_is_rejected_not_dropped`, `test_unknown_outer_key_counts_and_blocks_the_batch` |
| C8 | `test_an_email_matching_two_staff_rows_is_ambiguous_*`, `test_a_single_case_variant_*`, `test_the_import_rejects_a_case_twin_email_as_ambiguous_*`, `test_a_member_renamed_between_resolve_and_merge_*` and `test_a_member_turned_client_between_resolve_and_merge_*` (two connections), `test_a_blocked_row_lock_is_rejected_as_lock_timeout_*` (two connections), `test_real_apply_locks_members_in_sorted_id_order`, `test_cards_are_written_in_resolved_member_id_order_*`, `test_a_member_that_no_longer_matches_*`, `test_apply_sets_a_transaction_local_lock_timeout_*` |
| C9 | `test_member_self_read_of_the_employee_row_has_no_notes`, `test_the_legacy_employee_shape_never_carries_the_profile` |
