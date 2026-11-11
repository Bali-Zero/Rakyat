# Staff HR card: contract

Surfaces: `GET /api/hr/me/card`, `GET|PATCH /api/hr/employees/{id}/card`,
`POST /api/hr/employees`, `backend/scripts/import_staff_cards.py`.
Storage: `hr_employees` scalar columns plus `hr_employees.profile` (jsonb object,
migration 353). Code: `hr_card_models.py`, `hr_service.py`, `routers/hr.py`.
Every clause below has a named test; the table at the end is the index.

**S1 No-echo.** On these surfaces no caller-supplied value and no stored personal value
may appear in an error response body, an exception message, a log record or an alert.
(a) A 422 carries only `type`, a SAFE `loc` and a fixed message. The `loc` is built from
KNOWN field names only: a path segment that is not a field declared by the model at that
depth, or a list index, becomes the literal `<key>` (extra keys and `profile.docs` slot
names are caller-controlled). (b) Every failure in card code becomes `HRCardError(code)`
with `code` from the closed enum `HRCardCode`, never a driver or pydantic message.
(c) The route layer maps codes to status codes and fixed detail strings.
(Successful reads of a card naturally return the stored values to their authorised reader.)

**S2 Reads never validate.** Stored data is read through READ models (plain optional
types) and coercion helpers that return None, never raise and never quote the value.
Write validators run only on input. A stored value the write validators would reject
today (a newline, an odd character) yields a 200 card with a string shown as stored; a
value of the wrong type is shown as null.

**S3 jsonb goes through the production codec exactly once.** The service passes Python
objects (dict) for jsonb parameters, as the rest of the codebase does with the production
pool (`backend/core/pg_json_codec.py`); never a pre-serialised JSON string, never a
`json.loads` of a value the codec already decoded. Scripts register the same codec on their
connection (`init_asyncpg_connection`). DB-backed tests build their pool with the same
registration (`backend/tests/fixtures/prod_shaped_pool.py`).

**S4 Merge semantics.** Omitted scalar columns and omitted profile keys are preserved.
`profile`, `profile.docs`, `profile.contract` are objects: stored non-objects are treated
as `{}` before merging; an incoming non-object is rejected (`profile_not_object`). `docs`
and `contract` merge key by key. After a merge `jsonb_typeof(profile) = 'object'` is
asserted in the SQL (`RETURNING`); anything else fails and rolls the write back.

**S5 PATCH is one transaction.** Lock, write, read back, build the response, THEN commit
(`HRService.patch_card`). A failure at any step persists nothing.

**S6 Import is all-or-nothing and accounts for every card.** Each card ends in exactly one
of `applied` (the merge returned the row id), `rejected:<code>`, `not_found`. `--apply`
commits only if every card is applied (`not_found` tolerated with `--skip-not-found`),
printing `committed: yes|no`; a merge that returns no row is `rejected:not_applied` and
rolls the batch back. The insert path never silently does nothing: on a concurrent row it
falls through to the update. Dry-run writes nothing and reports what it can know
(`rejected:*`, `not_found`, `would_update`, `would_insert`).

**S7 Validation accepts real data.** Names with apostrophes, dots, hyphens; multi-word
bank names; NPWP with 15 or 16 digits and punctuation; account numbers with spaces or
dashes; addresses with slashes, commas and line breaks (`\n` in `address` only; every
other control or format character is rejected everywhere).

**S8 Scope of access is unchanged.** A member reads only their own card; admin routes are
admin-only; `notes` never leaves in a member-facing response. The self lookup compares
`lower(email)` on both sides. The legacy employee routes do not serve `profile`.

## Proof index

| Clause | Tests |
|---|---|
| S1 | `test_hr_staff_card.py`: `test_422_loc_*`, `test_post_employees_422_*`, `test_a_database_error_*`; `test_import_staff_cards.py`: `test_output_carries_counts_only` |
| S2 | `test_hr_staff_card.py`: `test_get_card_with_a_newline_*`, `test_get_card_with_a_json_string_profile_*`, `test_the_card_response_cannot_fail_*` |
| S3 | `test_migration_353_hr_employees_profile.py`: `test_two_patches_keep_profile_an_object_under_the_production_codec` |
| S4 | `test_migration_353_*`: `test_nested_merge_*`, `test_a_non_object_profile_*`, `test_a_non_object_incoming_*` |
| S5 | `test_migration_353_*`: `test_patch_card_rolls_back_when_the_view_fails` |
| S6 | `test_migration_353_*`: `test_import_*`; `test_import_staff_cards.py` |
| S7 | `test_hr_staff_card.py`: `test_real_data_shapes_are_accepted`, `test_multiline_address_*` |
| S8 | `test_hr_staff_card.py`: `test_member_*`, `test_non_admin_*`; `test_migration_353_*`: `test_self_lookup_is_case_insensitive_and_own_only` |
