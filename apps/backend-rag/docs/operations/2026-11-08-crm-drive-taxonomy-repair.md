# CRM Drive canonical taxonomy repair

Status: implementation approved by the owner on 2026-11-08. Production track: Bali-Zero/Rakyat. BLUE mission, external Codex builder; independent Claude gate required before landing.

## Consumer and problem

The November 5 reorganization established four top-level folders per person/company. November 7 metadata inspection found legacy personal folders again. CRM upload, provisioning, portal fallback and maintenance writers still use older names. Kita lists physical Drive folders while PostgreSQL stores root/subfolder/document references separately.

## Canonical contract

Individual: `01_Profile`, `02_Immigration`, `03_Company_and_Tax`, `04_Archive`.
Company: `01_Legal_and_Licensing`, `02_Tax_Returns_and_Payments`, `03_Finance_and_Banking`, `99_Archive`; existing `00_Credentials` is a permitted exception, never opened by repair.

Individual aliases: `00_Profile` and `04_Family` -> `01_Profile`; `01_Immigration` -> `02_Immigration`; `02_Company`, `03_Tax`, `03_Company&TAX` -> `03_Company_and_Tax`; `99_Misc` -> `04_Archive`. Canonical names map to themselves. Company aliases follow migration350.

New writes use canonical names only. Provisioning paths are deduplicated in stable order when legacy categories converge. Preserve nested directories, document/file IDs, root IDs, access controls and document visibility/state. Legacy paths remain readable. A combined company/tax folder alone does not establish tax classification; filename/document evidence has priority, otherwise retain unknown/other.

A cached destination is trusted only if metadata establishes an active physical folder with the expected canonical name under that entity root. Reject shortcut destinations and ambiguous duplicate canonical folders; never choose the first arbitrary match. Legacy folders may coexist until reconciliation; this taxonomy change must not introduce implicit moves/deletions of those folders. Preserve existing locks and immediate root persistence. Preserve unrelated pre-existing document lifecycle behavior, including visa rotation; only canonical destination resolution may change where necessary. The repair does not add, remove or reinterpret document visibility/state transitions.

## Data reconciliation boundary

Deploy writer correction first. Then create a bounded idempotent repair plan from fresh metadata and authoritative application mappings, recording expected pre-state and rollback inverses. Apply only if pre-state still matches. Database mapping writes use backend code, never ad hoc SQL. No bulk NULL, DELETE/TRUNCATE, guessed name-only links or credential-content reads.

Canonical mapping upserts and same-entity organization are in scope. Cross-entity reassignment requires decisive identifiers. Ambiguous ownership, duplicate canonical destinations, changed metadata or broader effective destination access fail closed for that item; independent items proceed. Reports use client IDs/hashes and aggregates, never client PII.

## Acceptance

- All active writers choose four canonical parents; converging categories and company/individual contexts are tested.
- Mixed canonical/legacy structures write canonical and preserve historical references.
- Stale, moved, trashed, shortcut or duplicate destinations cannot silently receive uploads.
- Polling/backfill understands old and new names without inventing company/tax classification.
- Focused tests and hosted Actions pass; fresh cross-family gate signs the exact head before merge.
- Deployed code and post-repair metadata demonstrate canonical behavior and mapping agreement for repaired roots.
- Unresolved identity/business exceptions remain explicit; a sample or successful process is not whole-archive certification.

## Operator entrypoint and limits

Run `python -m backend.scripts.crm_drive_reconcile --entity-kind individual --root-folder-id <approved-root>` inside the deployed backend environment. Supply each approved root explicitly (maximum 100). Company roots use `--entity-kind company`. The default is read-only and validates SQL planning without executing its upsert. Apply the exact reviewed digest with `--apply --expected-plan-sha256 <digest>`; changed inventory or mappings invalidate it. `--file-id <approved-id>` is a separate read-only ownership inspection mode.

Plans and successful results contain canonical category constants, entity IDs, opaque old/new Drive IDs and CAS inverse instructions. Reverse mapping checks cover both tables; shared roots and foreign owners fail closed. Legacy mapping rows are retained. The tool performs no Drive mutations and no root rebinding. Clients typed as company are outside these repair cohorts; companies are resolved through the companies table. Unbound roots require separate decisive identity evidence. Inserts use the migration062 created_at default.

Writer locks serialize application callers by parent/name across service-account, OAuth and Guardian paths, including nested visa directories. Direct external edits in Drive do not participate in these PostgreSQL locks. Lookup/index lag fails closed and may require a later retry. Existing visa rotation behavior is preserved; its previously inconsistent service-account move signature is outside this taxonomy repair.

## Execution ledger

2026-11-08: owner-authorized implementation in an isolated Rakyat worktree. Integrated local validation: 589 tests passed, changed Python files passed Ruff check/format, and diff whitespace validation passed. Independent pre-review requested B1 ownership uniqueness, B2 concurrent creation serialization and B3 reversible mapping evidence; all three were addressed with regression coverage. Final exact-head cross-family gate, hosted Actions, deployment and live cohort reconciliation remain pending and must be evidenced separately.
