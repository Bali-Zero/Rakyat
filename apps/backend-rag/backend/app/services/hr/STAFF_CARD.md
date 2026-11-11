# Staff HR card: read contract

Storage: `hr_employees` scalar columns plus `hr_employees.profile` (jsonb object,
Migration 354). Code: `hr_card_models.py`, `hr_service.py`, `routers/hr.py`. Every clause has a
named test; the index is at the end.

## Scope

In: the column `hr_employees.profile` and ONE read route, `GET /api/hr/me/card`, through which a
staff member reads their own card.

**Nothing in this change writes `profile`.** The column is added with the default `{}` and stays
`{}` for every row until a separate change that writes it lands. The route therefore returns the
existing scalar columns of the row and empty profile fields until then.

Not in this change, on purpose:
- **Any write path for card data**: the import script and HTTP writes alike. No PATCH, no POST,
  no admin card route. The legacy `POST /employees` is untouched (its response no longer carries
  `profile`).
- **Admin card routes** of any kind.
- **Deletion** of profile data.
- **Any new route with a path or query parameter.**
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

**C2 What the no-value guarantee covers.** The route has no caller-supplied input at all (no
path, query or body parameter), so card code has no caller value to echo. Card code
(`hr_card_models.py`, the card read function of `hr_service.py` and the route handler) adds no
personal value to any log record, exception text or response error: it raises `HRCardError(code)`
with a code from the closed enum `HRCardCode`, and the route maps it to a fixed text. The member's
own card is returned in full except HR free-text `notes`, which are not even selected (`notes`
never leaves).

Known pre-existing, not changed here (this PR neither introduces nor fixes them):
- Request-level logging by shared middleware (`middleware/activity_logging.py:189`,
  `services/monitoring/activity_logger.py:125` and `:350`) records unexpected query strings and the
  user-agent on every route, this one included. A guarantee about card code is not a guarantee about
  that layer.
- The older HR routes authorise from the token's claims (`routers/hr.py:156`,
  `hr_service.py:115`, `utils/hr_utils.py:42`); only the new `/me/card` route re-checks the stored
  row in SQL (C1).

**C3 Reads never validate.** The stored data goes through the READ model (plain optional types)
and coercion helpers that return None, never raise and never quote the value. Whatever shape a
value in the row has, the route answers a 200 card; a value of the wrong type is shown as null.

**C4 jsonb through the production codec.** DB-backed tests register the same connection codec as
production (`tests/fixtures/prod_shaped_pool.py`), so the stored profile is read the way the
running service reads it.

**C5 Legacy routes.** `GET /employees/{id}` drops `notes` for a non-admin self-read; no legacy
employee read or write response carries `profile`.

## Clause-to-test index

| Clause | Tests |
|---|---|
| C1 | `unit/app/routers/test_hr_staff_card.py` (keyed by id, client 403, 404, one GET without parameters); `unit/app/services/hr/test_hr_card_service.py::test_get_card_for_member_is_keyed_by_primary_key_*`; `tests/migrations/test_migration_354_hr_employees_profile.py`: `::test_me_card_returns_the_authenticated_rows_card_never_the_case_twins`, `::test_get_card_for_member_is_primary_key_only_and_staff_only`, `::test_get_card_for_member_requires_an_active_member`, and through the real middleware with signed tokens `::test_real_auth_path_active_staff_gets_own_card`, `::test_real_auth_path_a_deactivated_member_with_a_valid_token_gets_404`, `::test_real_auth_path_a_staff_claim_on_a_client_row_gets_no_card`, `::test_a_client_claim_is_refused_and_a_forged_id_matches_no_row` |
| C2 | `test_a_database_failure_is_a_fixed_500_*`, `test_a_read_failure_is_a_code_*`, `test_member_gets_own_card_in_full_without_notes`, `test_the_surface_added_is_one_get_*` |
| C3 | `test_a_non_object_stored_profile_still_reads`, `test_odd_stored_strings_*`, `test_the_read_model_never_raises_*`, `test_a_stored_string_profile_and_odd_contract_still_read` |
| C4 | every DB-backed test in `tests/migrations/test_migration_354_hr_employees_profile.py` (its pool registers the production codec), and `test_migration_adds_the_column_and_rolls_back` for the column itself |
| C5 | `test_member_self_read_of_the_employee_row_has_no_notes`, `test_admin_read_of_the_employee_row_still_has_notes`, `test_the_legacy_employee_shape_never_carries_the_profile` |
