# Staff HR card: contract

Storage: `hr_employees` scalar columns plus `hr_employees.profile` (jsonb object,
Migration 354). Code: `hr_card_models.py`, `hr_service.py`, `routers/hr.py`, and the import tool
`backend/scripts/import_staff_cards.py` (launcher) with `backend/scripts/_import_staff_cards_worker.py`
(worker). Every clause has a named test; the index is at the end.

## Scope

In: ONE read route, `GET /api/hr/me/card`, through which a staff member reads their own card, the
import tool through which HR fills cards (JSON on stdin), and ONE write route,
`PATCH /api/hr/people/{team_member_id}/card`, through which the global admins edit, clear or create
any person's card (C11), and the document door (`POST|DELETE /api/hr/people/{team_member_id}/documents/{slot}`),
the only route that writes a file id (C13). The read side is clauses C1-C4 and C9; the write side is
C5-C8 and C10-C13.

`GET /api/hr/me/overview` (`hr_overview_models.py`, `hr_overview_logic.py`) returns this same
card plus the member's payslips, bonuses, leave and attendance for the "My HR" page. It takes no
parameter either, resolves the member exactly as C1 does (the token's `sub`), reuses
`get_card_for_member` and `member_card` unchanged, and echoes the `sub` as `team_member_id` so the
page can check the owner of the answer against the bearer it sent.

Explicitly out, on purpose:
- **Any HTTP write of card data other than C11's route and C13's document door.** No other POST, no other PATCH. The legacy
  `POST /employees` is untouched (its response no longer carries `profile`).
- **Any other new route with a path or query parameter** (C11's and C13's are the exceptions).
- **Deleting profile data outside C11.** The import and `merge_card_on` without `clear` only add or
  overwrite. C11 removes a key only when the caller names it under `clear`, from a closed list.
- **Documents outside C13.** C11 never writes `profile.docs` or `profile.contract.file_id`; only the
  C13 door does. Bulk upload, OCR and an employee-facing viewing proxy are not part of it.
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
(`hr_card_models.py`, the card functions of `hr_service.py`, the route handler and the import
worker) adds no personal value to any exception text or response error it creates: it raises
`HRCardError(code)` with a code from the closed enum `HRCardCode`, and the route maps it to a fixed
text. What the import tool as a whole may show an operator is a stronger, separate guarantee with
its own limits (C10). The member's own card is returned in full except HR free-text `notes`, which
are not even selected (`notes` never leaves).

Known pre-existing, not changed here (this PR neither introduces nor fixes them):
- Request-level logging by shared middleware (`middleware/activity_logging.py:189`,
  `services/monitoring/activity_logger.py:125` and `:350`) records unexpected query strings and the
  user-agent on every route, this one included. A guarantee about card code is not a guarantee about
  that layer.
- The older HR routes authorise from the token's claims (`routers/hr.py:156`,
  `hr_service.py:115`, `utils/hr_utils.py:42`); only the new `/me/card` route re-checks the stored
  row in SQL (C1).

**C3 Reads never validate.** The stored data goes through the READ model (plain optional types)
and coercion helpers that return None, never raise and never quote the value. A stored value the
write validators would reject yields a 200 card; a value of the wrong type is shown as null.

**C4 jsonb through the production codec exactly once.** Python dicts are bound to jsonb
parameters; never a pre-serialised string. The import worker registers the codec on its
connection (`init_asyncpg_connection`); DB-backed tests use the same registration
(`tests/fixtures/prod_shaped_pool.py`), so the stored profile is written and read the way the
running service does.

**C5 Merge semantics** (`HRService.merge_card_on`). Omitted scalar columns and omitted profile
keys are preserved. `profile`, `profile.docs`, `profile.contract` are objects: a stored non-object
is treated as `{}` before merging; an incoming non-object is rejected (`profile_not_object`)
before any write. `docs` and `contract` merge key by key. `RETURNING jsonb_typeof(profile)` is
checked; anything but an object rolls the write back. A missing row is inserted (needs
`hire_date`); if a concurrent writer inserts first, the insert conflicts and falls through to the
update.

**C6 Write validators (import only).** `nik` 16 digits after removing spaces, dots, dashes;
`npwp` 15 or 16 digits likewise; BPJS Kesehatan 13 digits; BPJS Ketenagakerjaan / KPJ 11
alphanumerics (separators removed); bank account 6-20 digits after removing spaces and dashes.
Validation runs on the normalised form; the trimmed value is stored as given. `profile`, `docs`,
`contract` must be objects when present: an explicit `null`, `[]`, `false`, `0` or `""` is rejected
as it came, never normalised to `{}`. Unknown keys inside `profile`, `contract` and the card
fields are rejected (`extra=forbid`); unknown keys on the outer import card and a `profile` nested
inside `fields` are rejected by the import itself (C7). `is_active` is a strict boolean.
The CUSTOM validator messages never include the input. Pydantic's own `str(ValidationError)`
DOES render the input, and a deep error `loc` can hold a caller-supplied dict key, so no code
renders a `ValidationError`: the import worker reads `errors(include_input=False)` and only the
top-level `loc[0]`, and maps it to a closed reason code.

**C7 Import is all-or-nothing with full accounting.** Dry-run by default; `--apply` is one
transaction that commits only when every card is applied (`not_found` tolerated with
`--skip-not-found`); otherwise the whole batch rolls back. Every card ends in exactly one of
`applied`, `not_found`, `rejected:<code>`; if an unexpected failure interrupts the batch, every
card without a final answer is counted under `unclassified` (and the batch is rolled back), so
the totals always equal the number of cards. A merge that returns no row is `rejected:not_applied`
and the batch rolls back. An unknown key on the OUTER card, or a `profile` inside `fields`, is
`rejected:unknown_key`, never dropped. A JSON document of the wrong shape is
`rejected:bad_document`. A dry-run writes nothing and reports what it can know (`would_update`,
`would_insert`, `rejected:<code>`, `not_found`, and `blocked:not_found` when `--apply` would stop on
it). The worker reports the moment the transaction is decided: right after COMMIT or ROLLBACK
returned, and before the connection is closed (C10). A COMMIT or ROLLBACK that raises is never
reported as decided.

**C8 Import identity, locking and order.** The card's email is NEVER normalised: it is used
byte for byte for the exact match and for the lock re-check. A card whose email has leading or
trailing whitespace, any whitespace, any control / format / separator character, or is not a plain
single-token `local@domain` address (or is over 320 characters) is
`rejected:email_noncanonical` before any lookup. A card is resolved by EXACT email on staff rows
(`role <> 'client'`). The ambiguity check compares a normalised form that is case-folded AND has
every whitespace character removed (`lower(regexp_replace(email, '[[:space:]]', '', 'g'))`, both
sides): if it matches more than one staff row (a case twin, a padded twin) the card is
`rejected:ambiguous_email`; the import never picks one. A single row whose stored email differs
from the card's by case or padding is `not_found`. In `--apply`, before each merge the resolved
`team_members` row is locked `FOR SHARE` with the exact email and staff condition re-checked in
the same statement: a member renamed or turned into a client since the resolve is
`rejected:member_changed` and nothing is written. Cards are written in sorted resolved-member-id
order (document order for the same member), so two batches take member locks in the same order.
The transaction sets a local `lock_timeout` (5 s); a lock timeout or deadlock is
`rejected:lock_timeout` and the batch rolls back. This does not make concurrent batches
serialisable in general; it bounds the wait and makes the order deterministic.

**C9 Legacy routes.** `GET /employees/{id}` drops `notes` for a non-admin self-read; no legacy
employee read or write response carries `profile`.

**C10 The process boundary: what the operator's terminal can ever show.** The previous design
promised that no personal value reaches stdout or stderr and tried to keep that promise inside
one Python process (argparse echo, driver tracebacks at connect and close, library warnings,
output at import time, `SystemExit` passing a guard, a failing excepthook). In-process guards do
not close that class, so the guarantee is a PROCESS BOUNDARY, in two files:

- `import_staff_cards.py` is the LAUNCHER and the only thing an operator runs, as
  `python -I backend/scripts/import_staff_cards.py [--apply] [--skip-not-found]`. It REFUSES to run
  unless `sys.flags.isolated == 1` (one line, `import_staff_cards: run with python -I`, exit 2),
  because the interpreter's own startup (`PYTHONWARNINGS`, `PYTHONSTARTUP`, user site) runs before
  any line of the file and cannot be policed from inside; under `-I` those and the cwd are ignored.
  It imports the standard library only (nothing that can emit output on import), parses its own arguments with a
  fixed-text parser (`--apply`, `--skip-not-found`, `--help`; any other argument is the one line
  `import_staff_cards: invalid arguments (run with --help)`, exit 2, never an echo), reads stdin as
  raw bytes without decoding, and starts the WORKER as a child with its stdout and stderr redirected to
  `os.devnull`, the bytes on its stdin and ONE extra inherited pipe on which the child writes its
  result. The exact command line is
  `<sys.executable> -I -c <fixed bootstrap literal> <ROOT> <ROOT>/backend/scripts/_import_staff_cards_worker.py <DEPS> --result-fd=<N> [--apply] [--skip-not-found]`,
  where ROOT is the `apps/backend-rag` directory computed from the launcher's own RESOLVED path and
  passed as an argv element (never interpolated into code); DEPS is ONE argv element (possibly the
  empty string): the launcher's own `PYTHONPATH` (read with `os.environ`; the interpreter ignores
  it under `-I`) reduced to the entries that are absolute paths of existing directories, resolved
  with `realpath`, without duplicates and without ROOT itself, at most 32 entries and 8192
  characters in all, joined with `os.pathsep`; anything else is dropped without a word. The
  bootstrap inserts ROOT at `sys.path[0]`, APPENDS the DEPS directories at the END of `sys.path`
  (after the interpreter's own entries) and runs the pinned worker file with `runpy.run_path`.
  The production image keeps the third-party packages (asyncpg, sqlalchemy, pydantic, ...) only
  in its `PYTHONPATH`, so without DEPS the worker cannot import them. The child is isolated
  (`-I`: no `PYTHON*` variable reaches the interpreter, no cwd, no user site; the venv's
  site-packages stay visible, so `-S` is not used), `close_fds=True` with only the result fd
  passed, and its own session (`start_new_session`, i.e. `setsid`): it has no controlling
  terminal, so a library cannot open `/dev/tty`. **The worker's APPLICATION code origin is the
  launcher's own tree and nothing else**: ROOT is first, so an unrelated `backend` package in a
  `PYTHONPATH` directory or in the cwd cannot replace it; a `sitecustomize` / `usercustomize`
  module in a DEPS directory is not imported (paths appended after startup are not searched for
  one). Its THIRD-PARTY dependencies are resolved from the interpreter's own site-packages and
  then from the DEPS directories, which therefore belong to the trusted prerequisite, together
  with the interpreter (see below).
- `_import_staff_cards_worker.py` is the WORKER (refuses to run without a result pipe). It does
  C5-C8 and writes up to three compact JSON lines on the pipe: `decided` (committed yes/no and the
  outcome counts), `failed` (a code) and `closed` (close ok yes/no). `decided` is written and
  flushed the moment the transaction is decided, before the connection is closed. Its exit code is
  advisory only.

The launcher installs its SIGINT / SIGTERM / SIGHUP handlers (they only set a flag) before it reads
stdin, reads at most 8 MiB (more is `input_too_large`, nothing ran) and applies ONE wall-clock limit
of 15 minutes to the stdin read and to the worker together. It reads the pipe with a 64 KiB cap; on
expiry, interrupt or any exceptional path it KILLS the child and then waits for it (bounded), and
prints ONLY what it rebuilds from a STRICTLY validated
result. Each line must match a closed grammar: no whitespace, phases `decided` / `failed` /
`closed` each at most once and nothing after `closed`, outcome keys from a compiled-in set of
literals, counts as non-negative integers up to 1,000,000, failure codes from a compiled-in set.
Anything else is discarded and counted as `protocol_error`. A second `decided`, a second `failed`,
any record after `closed`, or a `decided` that claims `committed` together with a rejected, blocked
or unclassified card contradicts itself: the launcher then believes NEITHER answer, reports
`committed: unknown` (for `--apply`) with `protocol_error`, and exits 1. The launcher never prints a byte that
came from the child. Everything it prints is one of its own literals or an integer.

*The guarantee, exactly:* the launcher writes only launcher-owned literals and integers; the
worker's streams are discarded; therefore no value from input, database, driver or library can
reach the operator's terminal through this tool. Operator visibility is by fixed code only. Any
diagnosis beyond the code means re-running against a staging database, by design: there is no
option that routes child output to the operator, and none will be added.

*What it does NOT cover:*
- the interpreter's own startup. A system-wide `sitecustomize` or `.pth` inside the interpreter's
  own site-packages, or a modified interpreter, runs before the launcher and in the worker too:
  the interpreter is a TRUSTED PREREQUISITE (`-I` removes the environment and the user site, not
  the installation);
- the directories named in the operator's `PYTHONPATH`. They are where the worker's third-party
  dependencies may come from, so they are part of the same trusted prerequisite: a hostile package
  in one of them can do anything the interpreter's own site-packages could (it cannot replace
  `backend`, ROOT being first, but it can shadow any dependency the interpreter's own site-packages
  does not hold);
- the launcher being killed. A SIGKILL cannot be handled and a SIGHUP / SIGTERM handler only sets a
  flag; the worker, being in its own session, may be orphaned and finish a commit nobody reports.
  The final report writes are not supervised either. The window between COMMIT returning and the
  `decided` record being written yields `unknown`, never a wrong answer;
- the shell history and process list of the command line that invoked the tool (the card
  document travels on stdin so no value is in argv, but whatever the operator types is theirs);
- the operator's own input file and the shell's handling of it;
- the database server's own logs. Every card statement binds its values as parameters (the SQL
  text carries `$n`, never a value; the only interpolated item is the integer lock timeout), so
  the statement text in a server log holds no personal value. But a server set to log
  statements (`log_statement`, `log_min_duration_statement`) writes the bound parameters in the
  DETAIL line unless `log_parameter_max_length` is limited (its default logs them in full), and
  a constraint violation in any statement logs "Failing row contains ..." with the row. By
  default parameters are not logged on error (`log_parameter_max_length_on_error = 0`). Check on
  the target server with `SHOW log_statement; SHOW log_min_duration_statement; SHOW
  log_parameter_max_length;` before an `--apply` against production;
- anything that reads the database afterwards (this is a write tool; the data is meant to be
  there).

*Launcher failure lines* (stderr, `import_staff_cards: failed (<code>)`, a closed set). Launcher:
`spawn_failed`, `timeout`, `interrupted`, `worker_killed`, `no_result`, `protocol_error`,
`worker_died_after_decision`, `close_failed`, `worker_exit_nonzero`, `output_closed`,
`input_too_large`, `internal_error`. Carried from the worker: `import_failed`, `bad_input`, `connect_failed`,
`unexpected`, `db_error`, `lock_timeout`, `hire_date_required`, `invalid_input`,
`profile_not_object`. Outcome keys on stdout: `resolved`, `not_found`, `applied`, `would_update`,
`would_insert`, `blocked:not_found`, `unclassified`, and `rejected:` plus one of `bad_shape`,
`unknown_key`, `email_missing`, `email_noncanonical`, `invalid_ptkp`, `invalid_profile`, `invalid_fields`, `ambiguous_email`,
`member_changed`, `not_applied`, `bad_document`, `hire_date_required`, `invalid_input`,
`profile_not_object`, `db_error`, `lock_timeout`. stdout is always `mode: apply|dry-run`, the
counts, then `committed: yes|no|unknown`.

*Truthfulness.* The launcher's report is truthful GIVEN that the worker process is the pinned
worker file of the launcher's own tree, run by a trusted interpreter. A replaced worker is
PREVENTED by the code-origin pinning above, not detected: the launcher trusts a well-formed
record from its pinned worker (the stub-worker tests are protocol tests and say only that). Given
that, whatever happens to the child, `decided` is the only thing that lets it say yes or no:

| # | What the launcher received | `committed:` | Failure line(s) | Exit |
|---|---|---|---|---|
| 1 | `decided`, `closed` ok, child exit 0, committed, no rejected card | yes | none | 0 |
| 2 | `decided` with a rejected card (rolled back), `closed` ok, exit 0 | no | none | 1 |
| 3 | `decided`, `closed` with close_ok false | as decided | `close_failed` | 1 |
| 4 | `decided`, then no `closed` (child crash, signal, plain exit) | as decided | `worker_died_after_decision` | 1 |
| 5 | `decided`, then timeout / interrupt / protocol error | as decided | `timeout` / `interrupted` / `protocol_error`, then `worker_died_after_decision` | 1 |
| 6 | `decided`, `closed` ok, child exit non-zero, no `failed` record | as decided | `worker_exit_nonzero` | 1 |
| 7 | `decided` (rolled back after an error) then `failed` | no | the carried code | 1 |
| 8 | worker found nothing begun (bad input, connect failure, codec failure): `decided` no with no counts, then `failed` | no | the carried code | 1 |
| 9 | no `decided`, `failed` only (e.g. COMMIT raised) | `unknown` (`--apply`) | the carried code | 1 |
| 10 | no `decided`, no `failed`, child killed by a signal | `unknown` | `worker_killed` | 1 |
| 11 | no `decided`, no `failed`, child gone or silent | `unknown` | `no_result` | 1 |
| 12 | no `decided`, timeout / interrupt / protocol error | `unknown` | `timeout` / `interrupted` / `protocol_error` | 1 |
| 13 | worker never started: spawn failure, stdin unreadable, over the cap, a timeout or an interrupt while reading stdin | no | `spawn_failed` / `bad_input` / `input_too_large` / `timeout` / `interrupted` | 1 |
| 13b | a second `decided` or `failed`, a record after `closed`, or a self-contradicting `decided` | `unknown` (`--apply`) | `protocol_error` | 1 |
| 14 | the launcher's stdout is closed | as in the row that applies, written to stderr | the row's lines, then `output_closed` | 1 |
| 15 | the launcher is not isolated (no `-I`) | not reported | `import_staff_cards: run with python -I`, nothing ran | 2 |
| 16 | any exception or `SystemExit` anywhere in the launcher after argument parsing | not reported | `internal_error` (written with `os.write`, then `os._exit(1)`) | 1 |

`unknown` means the commit may or may not have happened and the launcher does not claim either.
For a DRY-RUN `committed:` is always `no` (nothing is ever written) and the exit code is 0 when
`decided` and `closed` ok arrived and the child exited 0, rejected cards notwithstanding. A SIGINT
or SIGTERM sets a flag that the supervising loop reads (no exception is raised at a random point),
and signals are ignored while the final report is written, so an interrupt cannot cost the operator
the truth about a decided batch. The test-only environment variable
`IMPORT_STAFF_CARDS_TEST_TIMEOUT_S` can only SHORTEN the wall-clock limit (a value of 0.2 s up to
below 900 s is honoured; anything else, including 0, negatives, `nan`, `inf` and larger values, leaves
the default). It changes nothing else.

**C11 The HR write route** (`PATCH /api/hr/people/{team_member_id}/card`,
`routers/hr_people.py`, body `hr_card_edit.CardEditBody`, service `HRService.edit_card`). Added
2026-11-12 on the owner's order that the global admins can edit any employee's card.
- *Who.* `can_write_hr_card(user)` (`utils/hr_utils.py`): the email is in
  `settings.admin_emails_set` (the owner and Asya) and the role claim is not `client`. NOT
  `HR_EXTRA_ADMIN_EMAILS`, NOT a scoped viewer, NOT a member; the `role` claim grants nothing.
  Everyone else gets 403 before any lookup. `GET /api/hr/me/capabilities` reports it as
  `hr_card_write`.
- *Which person.* The path id must be a UUID and a visible person by the People predicate
  (`VISIBLE_PERSON_PREDICATE`): a client, monitoring, partner, inactive or unknown account is 404.
  The member row is then locked `FOR SHARE OF tm` in the write's own transaction, and that SAME
  statement re-checks the full `VISIBLE_PERSON_PREDICATE` (imported, not copied) and the exact stored
  email, under the same 5 s `lock_timeout`: a member deactivated, or turned monitoring / partner /
  client, after the route's lookup is 404 and nothing is written; a concurrent
  `UPDATE team_members SET active = false` waits for the edit's commit. Create mode passes the
  predicate through its company-address-plus-named-role branch exactly as the roster shows that
  person. A lock timeout is 409 `Card is busy, try again`.
- *Body.* `{"card": StaffCardPatch, "clear": [...]}`, `extra=forbid` at every level. `card` is the
  import's `StaffCardPatch` unchanged (C6 validators, not loosened). `clear` is a closed list:
  `npwp`, `bpjs_kesehatan_number`, `bpjs_ketenagakerjaan_number`, `bank_name`, `bank_account`,
  `bank_account_holder`, `legal_name`, `nik`, `birth_place`, `birth_date`, `gender`, `address`,
  `religion`, `marital_status`, `employer_entity`, `unit`, `end_date`, `contract_number`. Not
  clearable: `hire_date` (a record always has one), `is_active` (a boolean you set), `ptkp_status`
  (NOT NULL with a default), documents. 422 when a key is both set and cleared, when a set key is an
  explicit `null` (a null sets nothing; name the key under `clear`), when `docs` or
  `contract.file_id` is sent, when a key repeats under `clear`, or when the body changes nothing.
- *Semantics.* The write is `merge_card_on` (C5) with its `clear` argument, one statement in one
  transaction: omitted keys are preserved, `docs` and `contract.file_id` are untouched, a cleared
  column becomes a real NULL, a cleared profile key is REMOVED from the jsonb (not set to null),
  `contract_number` removes `profile.contract.number` only. A person with no record and no
  `hire_date` is 422 `hire_date_required` and nothing is written; with `hire_date` the record is
  created (insert with `base_salary_idr` 0, `TK/0`, active unless `is_active` says otherwise; a
  concurrent insert falls through to the update). Two concurrent edits of one member serialise on
  the row lock and neither loses a key the other did not touch.
- *Answer.* The person's refreshed `PersonOverview` (the shape `GET /api/hr/people/{id}/overview`
  gives, whose `card` is the updated card), `viewer_team_member_id` = the bearer's `sub`.
- *No value, ever (C2 extended).* Errors are fixed codes. A validation error is NOT FastAPI's
  default 422 (which echoes `input`): the route reduces it to
  `{"detail": [{"field", "code", "message"}]}`, `field` from the closed key set above (or empty), `message`
  from a fixed table, never an `input`, `ctx` or caller-invented key. Service failures are
  `HRCardError(code)` mapped to a fixed response (409 / 422 `hire_date_required` / 500); the 422 is
  returned as a response, not raised as `HTTPException`, because the app's global handler flattens a
  list `detail` to a string. Nothing reaches Sentry either: the FastAPI integration copies the request
  JSON onto an event, so `sentry_config._before_send_impl` replaces `request.data`, `query_string` and
  `cookies` of every `/api/hr/` event by the placeholder, by ROUTE whatever the keys (transaction path
  included), and the HR card keys are also exact-match redaction keys. The log carries
  only the actor's team_member id, the target team_member id and the KEY NAMES set or cleared. No
  audit table is written: the repo has none for HR (`crm_audit_log` is CRM-entity specific) and a
  new one is a separate decision.
- *Two doors, one rule.* The legacy `POST /api/hr/employees` also writes bank and tax columns, so it
  requires `can_write_hr_card` too (403 otherwise). The HR extra admin keeps reads and bonus powers,
  and neither route writes a card for them; the legacy Employees page gates its create and edit
  controls on `hr_card_write`.

**C12 `profile.on_payroll`: off-payroll staff** (added 2026-11-12 on the owner's order). A card key,
strict boolean (`StrictBool`: no "yes", no 1), absent means `true`. Only a stored JSON `false`
takes a person off payroll; the read side (`member_card`) and the SQL predicate
`hr_service.ON_PAYROLL_SQL` agree on that. The person keeps the card, the unit and the documents.
- *Write.* Two doors: the C11 route sets it (`profile.on_payroll`) and clears it (`on_payroll` is a
  `clear` key; clearing means back to the default, true), and the C10 import can set it because it
  shares the strict model (the operator door; it cannot clear). Nothing else writes it.
- *The payroll set.* `hr_service.payroll_set_sql()`: card active, login (`team_members.active`) active, role
  not client/monitoring/partner (`VISIBLE_PERSON_PREDICATE`), and the card not off payroll. Calculate,
  the approve drift check, the manual bonus and the employees flag all use it.
- *Bonuses.* `approve_bonus` refuses a person outside the payroll set (409 `off_payroll`) under the
  bonus row lock. The practice-completion hooks that insert a pending bonus are left alone: a pending
  bonus for an off-payroll person is harmless now that approve refuses it. `calculate_payroll` and
  `approve_payroll` read the set under `FOR SHARE OF e, tm`, so a concurrent card edit waits for the
  commit, and `approve_payroll` refuses (409 `payroll_roster_changed`) when the slip owners differ
  from the current set.
- *Who reads it.* `calculate_payroll` (excludes the person and, in the same transaction under the
  month lock, deletes the period's payslips of anyone outside the computed set unless the period was
  already approved or paid when found); `approve_payroll` (the bonus-drift check skips them);
  `create_manual_bonus` (refused, 409 `code: off_payroll`); `GET /api/hr/employees` (flag
  `on_payroll`, so the bonuses table and the Add-bonus list can leave them out);
  `/me/overview`, `/me/card`, `/my-dashboard`, `/bonuses` and `/payslips` for the person
  themselves (no payslips, no bonuses, no base salary; `card.on_payroll = false` is the explicit
  signal); the People console (chip, and the switch in the edit form). An HR viewer of the same
  person (`/people/{id}/overview`, the payroll and employees admin pages) sees exactly what they saw
  before. Leave and attendance are untouched.

**C13 The HR document door** (`POST` and `DELETE /api/hr/people/{team_member_id}/documents/{slot}`,
`routers/hr_people.py`, `hr_documents.py`). The one route that writes `profile.docs.<slot>` and
`profile.contract.file_id`; `slot` is one of `ktp`, `kk`, `npwp`, `bpjs_kes`, `bpjs_tk`, `contract`.
- *Who and what.* The global admins only (`can_write_hr_card`), checked before any lookup (403). The
  person must be a visible one (404) and have an `hr_employees` row (409 `hr_record_required`: add
  them to HR first). An unknown slot, an empty file, a file over 4 MB (the Vercel body limit) or one
  that is not PDF, JPEG or PNG by its MAGIC BYTES is a 422 (`%PDF-`, `FF D8 FF`, the 8-byte PNG
  signature). The client's MIME type and file name are never read. The body is touched only after
  the writer check, under a hard cap of 4 MB + 64 KiB (413 at once on a declared length over it, and
  as soon as a chunked body passes it), then parsed as multipart with exactly one part named `file`.
- *Where the file lives.* The owner's private tree `HR&FINANCE/HR/DOKUMEN KARYAWAN`
  (`settings.hr_documents_drive_root_id`), as `<UNIT>/<person display name>/`. UNIT is the card's
  `profile.unit` stripped and upper-cased, `LAINNYA` when absent; unit folders are found (or created)
  by exact name under the root. The person folder is found by its appProperty `nz_hr_member`; a
  folder found under another unit is MOVED to the current unit, never duplicated; one that is not
  found is created. Find-or-create runs under `pg_advisory_xact_lock(hashtext('hr_docs_folder:'||id))`
  FIRST, then the card's unit, name and record are read in that same transaction, then a per-unit
  lock is taken (always this order). Each request builds its own Drive client: googleapiclient is
  not thread-safe. Writes use the tree owner's delegated identity
  (`settings.hr_documents_drive_subject`): the service account alone is a reader there. Sharing is
  never changed and nothing is ever permanently deleted.
- *Upload.* Title `<LABEL> - <full name> - <YYYY-MM-DD WITA>.<ext of the DETECTED type>`, appProperties
  `nz_hr_member` and `nz_hr_slot`. Then ONE transaction: member lock (`lock_visible_person`), read the
  previous id, write the new id through `merge_card_on`, insert the audit row; any failure rolls all of
  it back and the just-uploaded file is trashed. A failure AT or after COMMIT proves nothing: the slot
  is re-read on a fresh connection; if it holds the new id the upload succeeded, if not the new file
  is trashed, if that cannot be told it is left in place. Drive failures are `502 drive_unavailable` with a
  fixed message; a lock timeout is 409 as in C11.
- *Trash only what is ours.* After the commit the replaced (or removed) file is moved to the Drive
  trash ONLY if its appProperties `nz_hr_member` equals this member, i.e. this door made it. A file
  anyone else put there is left untouched. The response is `{slot, file_id, previous}` with
  `previous` one of `trashed`, `kept`, `none`.
- *Audit and logs.* One `security_audit_log` row per change (`hr_document_upload` or
  `hr_document_remove`), in the same transaction as the card write, details = slot, the new id (made
  by this request) and `previous_sha` (first 16 hex of the sha256 of the old value, which is free
  text under C10 and so never stored in clear): never a file name or a person's name. A previous
  value Drive does not know is `kept`, silently. Logs carry actor id, target id, slot and outcome. The 422
  uses the no-echo route class of C11.

## Clause-to-test index

Paths are under `backend/tests/` unless given.

| Clause | Tests |
|---|---|
| C1 | `unit/app/routers/test_hr_staff_card.py` (keyed by id, client 403, 404, one GET without parameters); `unit/app/services/hr/test_hr_card_service.py::test_get_card_for_member_is_keyed_by_primary_key_*`; `tests/migrations/test_migration_354_hr_employees_profile.py`: `::test_me_card_returns_the_authenticated_rows_card_never_the_case_twins`, `::test_get_card_for_member_is_primary_key_only_and_staff_only`, `::test_get_card_for_member_requires_an_active_member`, and through the real middleware with signed tokens `::test_real_auth_path_active_staff_gets_own_card`, `::test_real_auth_path_a_deactivated_member_with_a_valid_token_gets_404`, `::test_real_auth_path_a_staff_claim_on_a_client_row_gets_no_card`, `::test_a_client_claim_is_refused_and_a_forged_id_matches_no_row` |
| C2 | `test_a_database_failure_is_a_fixed_500_*`, `test_a_read_failure_is_a_code_*`, `test_member_gets_own_card_in_full_without_notes`, `test_the_surface_added_is_one_get_*`, `test_merge_card_on_replaces_a_driver_error_*`, `tests/migrations/test_hr_card_write_path.py``::test_a_card_write_failure_carries_no_driver_text`, `test_a_card_error_is_accounted_by_its_code_only` |
| C3 | `test_a_non_object_stored_profile_still_reads`, `test_odd_stored_strings_*`, `test_the_read_model_never_raises_*`, `test_a_stored_string_profile_and_odd_contract_still_read` |
| C4 | every DB-backed test in `tests/migrations/test_migration_354_hr_employees_profile.py` (its pool registers the production codec); `tests/migrations/test_hr_card_write_path.py``::test_two_merges_keep_profile_an_object_under_the_production_codec`; `unit/scripts/test_import_staff_cards_worker.py``::test_work_registers_the_production_codec_on_its_connection` |
| C5 | `tests/migrations/test_hr_card_write_path.py``::test_nested_merge_survives_*`, `::test_a_non_object_profile_is_normalised_*`, `::test_merge_keeps_omitted_columns_*`, `::test_a_non_object_incoming_part_*`, `::test_a_profile_that_would_not_be_an_object_*`, `::test_a_concurrent_insert_falls_through_to_the_update_on_two_connections` |
| C6 | `unit/app/services/hr/test_hr_card_models.py` (`::test_the_custom_validator_messages_carry_no_input`, `::test_pydantic_renders_the_input_in_str_of_a_validation_error_*`); `unit/scripts/test_import_staff_cards_worker.py``::test_a_present_non_object_*`, `::test_a_non_object_docs_or_contract_*`, `::test_identifier_shapes_are_enforced_by_the_import` |
| C7 | `unit/scripts/test_import_staff_cards_worker.py``::test_apply_*`, `::test_dry_run_*`, `::test_every_card_is_accounted_for_exactly_once`, `::test_an_unknown_key_on_the_outer_card_*`, `::test_unknown_outer_key_counts_and_blocks_the_batch`, `::test_a_profile_nested_inside_fields_*`, `::test_a_merge_that_returns_no_row_*`, `::test_a_failing_lookup_still_accounts_for_every_card`, `::test_decided_is_reported_*`, `::test_a_commit_that_raises_is_never_reported_as_decided`, `::test_an_error_inside_the_transaction_is_decided_false_*`; `tests/migrations/test_hr_card_write_path.py``::test_import_apply_is_atomic_*`, `::test_import_apply_creates_a_card_with_a_hire_date`, `::test_import_second_merge_returning_none_*`, and through the real launcher `::test_launcher_dry_run_then_apply_writes_the_card_and_says_so`, `::test_launcher_a_rejected_batch_writes_nothing_and_says_committed_no` |
| C8 | `unit/scripts/test_import_staff_cards_worker.py``::test_an_email_matching_two_staff_rows_is_ambiguous_*`, `::test_a_single_case_variant_*`, `::test_cards_are_written_in_resolved_member_id_order_*`, `::test_a_non_canonical_email_is_rejected_before_any_lookup`, `::test_a_canonical_email_is_used_byte_for_byte`, `::test_a_non_canonical_card_never_reaches_the_database`, `::test_the_lock_recheck_receives_the_card_email_unchanged`, `::test_a_member_that_no_longer_matches_*`, `::test_apply_sets_a_transaction_local_lock_timeout_*`; `tests/migrations/test_hr_card_write_path.py``::test_the_import_rejects_a_case_twin_email_as_ambiguous_*`, `::test_a_padded_twin_and_a_case_twin_make_either_canonical_address_ambiguous`, `::test_a_padded_twin_alone_makes_the_canonical_address_ambiguous_*`, `::test_a_card_naming_the_padded_address_is_refused_*`, `::test_a_lone_padded_row_is_not_found_*`, `::test_launcher_a_padded_twin_is_ambiguous_through_the_whole_tool`, `::test_a_member_renamed_between_resolve_and_merge_*`, `::test_a_member_turned_client_between_resolve_and_merge_*`, `::test_a_blocked_row_lock_is_rejected_as_lock_timeout_*`, `::test_real_apply_locks_members_in_sorted_id_order`, `::test_two_concurrent_batches_over_the_same_members_in_opposite_order`, and through the real launcher `::test_launcher_an_ambiguous_email_is_rejected_and_nothing_is_written`, `::test_launcher_a_member_renamed_between_resolve_and_merge_is_rejected` |
| C9 | `test_member_self_read_of_the_employee_row_has_no_notes`, `test_the_legacy_employee_shape_never_carries_the_profile` |
| C10 | `unit/scripts/test_import_staff_cards.py` (real launcher in a subprocess): `::test_family1_*` (real worker, fake driver: warnings, import-time output, `logging.log(60, ...)`, exceptions at connect / codec / transaction / commit / close, `SystemExit(0)`, `SystemExit(<sentinel>)`, `SystemExit(7)`, SIGKILL before and after the decision, hang, SIGINT before and after the decision, bad and wrong-shape stdin), `::test_protocol_*` and `::test_family2_*` (stub worker: garbage, oversized, unknown key, code outside the set, negative, huge, sentinel in a string field, duplicate / contradictory / trailing records, raw bytes and flags, large input, `python -I -W ignore`, closed stdout), `::test_the_worker_comes_from_the_launchers_own_tree_*`, `::test_production_layout_*` (an interpreter with no third-party packages, the fake driver only in a `PYTHONPATH` directory outside ROOT: dry-run and apply), `::test_without_pythonpath_the_bare_interpreter_*`, `::test_a_backend_package_in_pythonpath_or_cwd_is_never_used`, `::test_sitecustomize_and_usercustomize_in_pythonpath_are_not_executed`, `::test_the_dependency_filter_*`, `::test_pythonpath_unset_or_empty_*`, `::test_pythonpath_dirs_are_appended_at_the_end_*`, `::test_a_nonexistent_pythonpath_entry_naming_a_sentinel_*`, `::test_a_launcher_without_dash_i_refuses_*`, `::test_pythonwarnings_with_and_without_dash_i`, `::test_the_worker_is_started_isolated_*` (exact command line, own session, only the result fd), `::test_the_worker_has_no_controlling_terminal_*` (pty), `::test_a_stdin_that_never_ends_*`, `::test_a_signal_while_reading_stdin_*`, `::test_an_input_over_the_cap_*`, `::test_an_exception_or_exit_anywhere_after_the_arguments_is_one_fixed_line`, `::test_every_parse_error_is_the_one_fixed_line_and_exit_2`, `::test_help_is_static_text`, `::test_the_launcher_imports_the_standard_library_only`, `::test_the_test_timeout_knob_can_only_shorten_the_deadline`, `::test_the_truthfulness_table_row_by_row`, `::test_exit_zero_needs_*`, `::test_a_result_is_taken_only_when_it_matches_the_closed_grammar`, `::test_a_spawn_failure_*`, `::test_the_launcher_refuses_without_the_isolated_flag_in_main`; the shared test root is `tests/fixtures/import_root.py`; `unit/scripts/test_import_staff_cards_worker.py``::test_the_records_the_worker_writes_are_exactly_what_the_launcher_accepts`, `::test_every_outcome_and_code_the_worker_can_produce_is_in_the_launcher_closed_sets`, `::test_work_*`, `::test_the_worker_refuses_to_run_without_a_result_pipe`, `::test_validate_card_reads_pydantic_errors_without_the_input` |
| C12 | `tests/migrations/test_hr_off_payroll.py` (real Postgres: calculate, stale slip, approved period, PATCH set and clear, manual bonus 409, my-dashboard, /me/overview); `tests/migrations/test_hr_card_edit_path.py::test_the_import_door_*`; `unit/app/services/hr/test_hr_card_models.py` (strict bool, read default) |
| C11 | `unit/app/routers/test_hr_people_card_edit.py` (authz matrix, 404, 422 matrix, no echo, fixed failures, keys-only log); `unit/app/routers/test_hr_capabilities.py::test_card_write_is_for_the_global_admins_only`; `unit/app/services/hr/test_hr_card_service.py::test_edit_card_*`, `::test_merge_card_on_passes_the_cleared_keys_*`; `tests/migrations/test_hr_card_edit_path.py` (real Postgres: set, clear, nested clear, docs untouched, create, serialisation, no value in logs, deactivated / monitoring / partner after the lookup, lock probe); `unit/app/setup/test_sentry_hr_request_redaction.py` (real SDK); `unit/app/routers/test_hr_staff_card.py::test_post_employees_is_for_the_global_admins_only` |
| C13 | `unit/app/services/hr/test_hr_documents.py` (fake Drive: unit and person folders, move, title, appProperties, trash-ours vs keep-foreign, compensation, magic bytes); `unit/app/routers/test_hr_people_documents.py` (authz incl. HR extra admin and scoped viewer, 404, 422s, 4 MB edge, fixed 409 / 502, no name echoed); `tests/migrations/test_hr_document_door_path.py` (real Postgres: card + audit in one transaction, rollback leaves neither) |
