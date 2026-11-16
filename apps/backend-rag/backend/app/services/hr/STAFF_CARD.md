# Staff HR card: contract

Storage: `hr_employees` scalar columns plus `hr_employees.profile` (jsonb object,
Migration 354). Code: `hr_card_models.py`, `hr_service.py`, `routers/hr.py`, and the import tool
`backend/scripts/import_staff_cards.py` (launcher) with `backend/scripts/_import_staff_cards_worker.py`
(worker). Every clause has a named test; the index is at the end.

## Scope

In: ONE read route, `GET /api/hr/me/card`, through which a staff member reads their own card, the
import tool through which HR fills cards (JSON on stdin), and ONE write route,
`PATCH /api/hr/people/{team_member_id}/card`, through which the HR writers (owner and accountant) edit, clear or create
any person's card (C11), and the document door (`POST|DELETE /api/hr/people/{team_member_id}/documents/{slot}`),
the only route that writes a file id (C13; same writers as C11). The read side is clauses C1-C4 and C9; the write side is
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
- **Documents outside C13.** C10 and C11 never write `profile.docs`, `profile.contract.file_id` or
  `profile.doc_proof` (C10 refuses the whole card, C11 the request); only the
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
2026-11-12 on the owner's order that the owner and the accountant can edit any employee's card;
narrowed 2026-11-13: global admin alone no longer suffices.
- *Who.* `can_write_hr_card(user)` (`utils/hr_utils.py`): the email is in
  the closed set `hr_utils.HR_WRITER_EMAILS` (the owner and the accountant, NOT every global admin:
  `ADMIN_EMAILS` also holds an IT account that must not write HR data) and the role claim is not
  `client`. The same set gates payroll calculate, approve and mark-paid (`can_run_payroll`, 403 for
  the rest; reads stay with the HR admins) and `GET /api/hr/me/capabilities` reports it as
  `hr_payroll_write`. NOT
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
  explicit `null` (a null sets nothing; name the key under `clear`), when `docs`,
  `contract.file_id` or `doc_proof` is sent, when a key repeats under `clear`, or when the body changes nothing.
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
`routers/hr_people.py`, `hr_documents.py`; spec "HR card document slots", v2.1). The ONE writer of
the six document slots (`docs.ktp`, `docs.kk`, `docs.npwp`, `docs.bpjs_kes`, `docs.bpjs_tk` and
`contract.file_id`; `slot` is `ktp`, `kk`, `npwp`, `bpjs_kes`, `bpjs_tk` or `contract`) and of
`profile.doc_proof`. Root cause of the earlier rounds: a stored slot value is untrusted free text,
yet every layer treated it as a Drive id. So provenance comes before display.
- *One writer (R1).* Only this door writes a slot value, the legacy `docs.contract` duplicate or
  `profile.doc_proof`. It writes only an id Drive returned for a file it created in this request;
  the one exception (N2) is the R5 job below, through the door's own write statement.
  The C10 import refuses a card carrying `docs`, `contract.file_id` or `doc_proof` as a whole, with
  the outcome `rejected:doc_keys_door_only` (in the launcher's closed outcome grammar), and writes
  nothing for it; `contract.number` and every other key import as before. C11 refuses the same keys.
  A slot is cleared only by this door's DELETE.
- *Provenance lives in the card (R2, N1, N4, N7, N8).* Each write sets, in the SAME `UPDATE` of the
  same row, the slot value and `profile.doc_proof.<slot> = {id, by, at}` (id = the new file id, `by`
  = the actor's email, `at` = UTC ISO time) through `jsonb_set`, per slot, so the other proofs and
  every other profile key survive. A remove clears the slot and sets `{id: null, by, at}`. A write or
  remove on `contract` also deletes `docs.contract`. A slot is VERIFIED iff its value is a JSON
  string equal to the JSON string `doc_proof.<slot>.id`: exact equality, no format check, no hash, no
  lookup. Nothing is hashed anywhere. There is NO audit-table write in the door's transaction
  (`security_audit_log` is created by v2 migration 359, but the door writes no audit row by design,
  SPEC v2.1: provenance is `doc_proof`): it touches only `hr_employees` (and the row
  lock on `team_members`). `doc_proof.<slot>` keeps the LAST change (who, when); no per-change
  history is kept, and no code consumes one.
- *Opaque until verified (R3).* An unverified slot value is never sent to Drive, logged at any
  level, placed in an error, a response or a Sentry event, or used to build a query, a URL or a Drive
  call. That includes the previous value of a replace or remove. The `googleapiclient.discovery` and
  `googleapiclient.http` loggers are held at WARNING, and `sentry_sdk.init` sets
  `max_request_body_size="never"`.
- *The API returns a derived map, never raw values (R4, N5, N6).* ONE pure projection,
  `hr_card_models.project_documents`, runs inside the shared card view (`member_card` /
  `_profile_view`), so every response that contains a card inherits it: `GET /me/card`,
  `GET /me/overview`, `GET /people/{id}/overview`, the C11 PATCH response (the person's overview)
  and, through `compose_member_overview`, every composer built on them. (`/employees` and
  `/employees/{id}` serve the legacy shape without the profile.) The card carries no `docs`, no
  `contract.file_id` and no `doc_proof`; `contract.number` stays. It carries `documents`, a closed
  object with exactly the six keys, each `{present, file_id}`: `present` is true iff the stored value
  is a non-empty string (a non-string or blank value is absent and never echoed); `file_id` is the
  value only when the slot is verified, else null. The roster's `papers_missing` counts `present`.
  The door's own responses are `{slot, documents}`: the six-key map after the write, and no
  `previous` anywhere.
- *Who and what.* The HR writers only (`can_write_hr_card`, the closed set `HR_WRITER_EMAILS`: not
  every global admin), checked before any lookup (403). The person must be a visible one (404) and
  have an `hr_employees` row (409 `hr_record_required`). An unknown slot, an empty file, a file over
  4 MB or one that is not PDF, JPEG or PNG by its MAGIC BYTES is a 422. The body is touched only after
  the writer check, under a hard cap of 4 MB + 64 KiB (413 on a declared length over it, and as soon
  as a chunked body passes it), then parsed as multipart with exactly one file part named `file`: no
  field, a closing boundary, a boundary of at most 70 characters, every parser error a fixed 422. The
  body is declared in OpenAPI through `openapi_extra`.
- *Where the file lives.* The owner's private tree `HR&FINANCE/HR/DOKUMEN KARYAWAN`
  (`settings.hr_documents_drive_root_id`), as `<UNIT>/<person display name>/`; UNIT is the card's
  `profile.unit` stripped and upper-cased (`LAINNYA` when absent). Unit and person folders are found
  (or created) by exact name / appProperty `nz_hr_member`; a person folder under another unit is
  MOVED, never duplicated. Lock order, everywhere: the member advisory lock
  (`hr_docs_folder:<id>`, the C11 edit takes it first too), then the row lock, then the per-unit
  advisory lock. Each request builds its own Drive client (googleapiclient is not thread-safe),
  as the tree owner's delegated identity (`settings.hr_documents_drive_subject`). Sharing is never
  changed and nothing is permanently deleted.
- *Upload and its failures.* Title `<LABEL> - <full name> - <YYYY-MM-DD WITA>.<ext of the DETECTED
  type>`, appProperties `nz_hr_member` and `nz_hr_slot`. One transaction: member lock, visibility
  lock, read the previous slot and proof in ONE statement, ONE UPDATE; any failure before the commit
  rolls back and the just-uploaded file is trashed. A failure AT or after COMMIT proves nothing: the
  slot (with its proof) is re-read under the same locks; if it holds the new id the upload succeeded,
  if not the new file is trashed, if that cannot be told it is left in place. After the commit the
  card's unit is read again under the member lock and the person folder moved if it changed. Drive
  failures are `502 drive_unavailable` with a fixed message; a lock timeout is 409 as in C11. A C11
  unit change does not move Drive folders: the folder is realigned by the next UPLOAD only, never by
  a DELETE (remove does not touch the folder). The reconciliation read returns the documents map
  from the same locked read that confirms the commit, so a landed upload always answers with the
  full six-key map.
- *Trash only what the door made (R6).* On replace or remove the previous file is moved to the Drive
  trash only if the previous value was VERIFIED when read under the lock AND Drive's own listing of
  this door's files (appProperties member AND slot, inside the person folder, not trashed, pdf, jpeg
  or png, never a folder) returns an entry whose `id` equals the proof id, compared locally; the
  LISTING entry's id is what is trashed, never the stored value. An unverified (legacy) value is
  never trashed, never queried, never sent anywhere: its remove clears the slot and makes no Drive
  call at all. Logs carry actor id, target id and slot, no value.
- *Legacy migration (R5, N2).* `backend/scripts/migrate_hr_legacy_documents.py` (operator, `python
  -I`, dry-run unless `--apply`) moves the slot values the C10 import wrote into this tree. Its input
  is `hr_legacy_documents_manifest.json`, frozen read-only before it (`[hr_employees.id, slot,
  sha256(value)]`, `docs.contract` folded into `contract`; built by
  `build_hr_legacy_documents_manifest.py`). A stored value goes to Drive only if its tuple is in the
  manifest AND it is id-shaped (`^[A-Za-z0-9_-]{25,100}$`, a letter); anything else is counted and
  left for a human. Per slot, under the member advisory lock held for the whole operation: re-read
  (still present-but-unverified, same hash; otherwise `changed`); reuse an earlier copy found by
  appProperties `nz_hr_member` + `nz_hr_slot` + `nz_hr_src` (= sha256(value)[:16]) in the person
  folder, so a resume needs no readable source; otherwise read the source's metadata (the dry-run
  stops before any write) and `files.copy` it there with the door's title and appProperties plus
  `nz_hr_src`; only a usable id string Drive returned is used (an unusable answer is
  `copy_unidentified`, the tagged copy reused by a later run). The job hands the door and itself ONE
  `ProtectedDrive` (SPEC v2.2 R5.1): every Drive mutation with a manifest source (or a non-id) in
  any id position - target, each parent removed, parent added, parent of a created or copied file
  - is refused before the call (`protected_refusal`), whichever lookup produced the id; a member
  whose folder step was refused is `folder:source_protected`. No id whose sha256 is any manifest
  digest is ever reused or written (R5.2: the boundary does not see the database). Then the door's
  UPDATE narrowed to a compare-and-set (the slot still holds that
  hash, unverified): no row is `conflict` and the copy is trashed by its own Drive id while the lock
  is held (`copy_left` counts a trash that failed); a database error before COMMIT re-locks,
  re-reads and trashes the copy unless the slot holds it; a failure at or after COMMIT is re-read
  under the door's locks (`uncertain` leaves the copy for the next run to reuse); a success is
  counted before the unit follow-up. The proof's `by` is `r5:migrate_hr_legacy_documents`. The
  source is only read and copied: its modifiedTime, parents and trashed flag are read once per
  distinct source before any Drive mutation of the run and again after the last slot
  (`acceptance5`: `observed` only when every slot the run migrated has its source baselined and
  audited unchanged). A read-only copy census counts every tagged copy of every manifest entry
  (`copy_held` / `copy_reusable` / `copy_residual`; a transport retry can make more than one;
  residuals are reported, never trashed; `copy_held` is the copy the slot holds, proof or not; a
  folder lookup or a listing the Drive client cut at its page cap is `census_skipped`, and the
  member's folder step and the reuse lookup refuse it the same way). SIGINT / SIGTERM / SIGHUP only set a stop flag (R5.3, a
  drain): the slot in flight ends by its normal path, no further slot starts (`not_started`), the
  audit and the census still run, `"run": "interrupted"`, exit 130. A watchdog thread outside the
  event loop bounds each unit of work (setup, baseline read, folder step, slot, audit read, census
  member, shutdown); past it the process writes its counts with `"run": "deadline"` and exits 124,
  nothing trashed; it exits 124 even when the line itself is what blocked. Each manifest entry
  counts exactly one outcome, settled once: a failed connection release, a cleanup or a deadline
  after it never re-classifies it, and a fatal error in a compensation or a reconcile still leaves
  that outcome (with `copy_left` / `uncertain`) on the line. The renderer checks the outcomes sum
  to the manifest. Containment: file
  descriptors 1 and 2 point at /dev/null for the whole run and the one output line, rebuilt from a
  closed grammar of codes and integers, leaves on a private duplicate of stdout (the manifest
  builder seals them the same way); Sentry must not be bound; every log record is counted and never
  written; the identifier loggers sit at WARNING.
- *Display.* The frontend links only `documents.<slot>.file_id`, counts `present`, and shows a
  present-but-unverified slot as "On file - not yet in DOKUMEN KARYAWAN" with no link and no value;
  Upload / Replace and Remove (when `present`) are for the HR writers.

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
| C13 | `unit/app/services/hr/test_hr_documents.py` (fake Drive: folders, move, title, appProperties, trash only a verified id the listing returns, no Drive call for an unverified one, compensation, commit unknown); `unit/app/routers/test_hr_people_documents.py` (authz incl. HR extra admin, scoped viewer and a global admin outside the writer set, 404, 422s, 4 MB edge, body cap before authz, parser holes, fixed 409 / 502); `unit/app/routers/test_hr_documents_projection.py` (every card response: closed six-key map, no stored value, no `docs` / `doc_proof` / `contract.file_id`); `unit/app/services/hr/test_hr_card_models.py`; `unit/scripts/test_import_staff_cards_worker.py` (`doc_keys_door_only`); `unit/app/routers/test_hr_people_card_edit.py` (C11 refuses the keys); `unit/app/setup/test_sentry_hr_request_redaction.py` (real SDK: body not read before the 403, no slot value in an event); `tests/migrations/test_hr_document_door_path.py` (real Postgres with NO audit table: slot and proof in one UPDATE, per-slot proofs kept, `docs.contract` deleted, verified vs unverified previous, commit unknown and reconcile under the locks, unit interleavings, real googleapiclient with HttpMockSequence at DEBUG, import refusing the door keys, a NIK-like value absent from the card view); `tests/migrations/test_migrate_hr_legacy_documents.py` (R5 on real Postgres + fake Drive: a NIK-like value outside the manifest or failing the fence never reaches Drive, a door write between copy and CAS is a `conflict` with the copy trashed, an uncertain commit then a rerun reusing the copy by `nz_hr_src`, `docs.contract` dropped, dry-run writes nothing, a second run changes nothing, an unreadable source left in place, real googleapiclient request shape, Sentry bound refuses, `-I` required; the trash on a conflict runs under the member lock and a failed one is `copy_left`, a CAS-step database error compensates, a landed commit whose answer was lost is `done`, a slot verified after the scan gets no Drive call, no manifest source is ever reused or trashed and a person folder that is one is never moved, a resume after an uncertain commit reuses the copy even when the source became unreadable, a bad copy id is never written, prints and tracebacks never reach fd 1/2, a failed import is `import_failed`; SPEC v2.2: the boundary refuses a protected id in every position with no call reaching Drive, rejects an unusable returned id, exposes nothing else, is the one instance job and door hold, and stops a move when consecutive lookups disagree or the unit follow-up meets a source; an unidentified copy is reused; a signal while a copy, a conflict trash, a compensation, a lost commit, a follow-up or the audit is in flight lets it finish and starts nothing after; a real SIGINT / SIGTERM / SIGHUP to a subprocess on the real entry path drains with exit 130; a success is counted before the follow-up; a `BaseException` at the CAS step compensates; the audit sees a changed source and counts distinct sources; SPEC v2.2 rev 3: a request blocked in its worker past the slot deadline ends the process with exit 124 and a rerun reuses its copy, a deadline in the re-read is `deadline`, a fatal line keeps `copy_left` and a confirmed `done`, a signal in the baseline or a folder step and the folder-code precedence, the census counts held, reusable and residual copies and skips an ambiguous folder, each failing step counts exactly one outcome, `acceptance5` per run; round 4: a failed release after the outcome does not re-classify it, a deadline after it adds nothing, a fatal in the compensation keeps `db_error` and `copy_left`, a fatal in the reconcile keeps `uncertain`, a fatal in the conflict trash stays `conflict` and compensates, a present `trashed` must be a real bool, a truncated listing is census-skipped and never reused, the real client signals a listing cut at its page cap, a line the channel never takes still exits 124; round 5: a copy the slot holds is held whatever its proof, a truncated folder lookup is census-skipped, the real client signals a folder lookup cut at its page cap; round 6: the census deadline bounds a whole member) |
