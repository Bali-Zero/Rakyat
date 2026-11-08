# Staff company Drive source index

Mandate: `kita-company-drive-sources-1108` · BLUE external builder.

The staff Company tab consumes `GET /api/crm/companies/{company_id}/drive-sources`.
The endpoint requires `require_team_member` explicitly; portal clients, partners,
machine accounts and unknown roles fail before company data is read.

## Contract

- Read stored `google_drive_folder_id`, `tax_dept_folder_id`, and
  `custom_fields.legacy_drive_folder_ids`. Return only generated Google Drive
  folder URLs and generic provenance kinds: primary, restricted tax, legacy.
- Deduplicate identical IDs while retaining every applicable source kind.
- Accept only 20–100 ASCII URL-safe ID characters with at least one letter;
  reject URLs, whitespace, empty/non-string/numeric values and common business
  identifier prefixes. This is conservative syntax validation, not verification
  that the target exists or is a folder. Google decides access and target validity.
- Never return raw custom fields, company names, invalid values, document lists
  or document counts. Never log malformed source values.
- Merge metadata is display-only. Accept an exact integer in the PostgreSQL
  integer range, distinct from the source ID, pointing to an existing terminal
  record. Booleans, strings, floats, missing targets, self-links, cycles and
  multi-hop chains are invalid. Chains are intentionally not traversed.
- Terminal records expose up to 100 directly incoming historical record IDs,
  ordered by ID. The exact-integer check is repeated after the JSONB comparison
  because JSONB numeric equality can equate integral floats and integers.

## Source/proxy boundary

Metadata is information, never an access grant. Links open Google Drive directly
with the viewer's existing Google permissions. No folder listing, proxy download,
permission change, document registration, synchronization, import or ingestion
is added. Historical records do not contribute documents or folders to a successor.
The existing primary-only sync action and document proxy remain unchanged.

The component appears only for the company already selected in the staff client
card. Companies without an active client link do not gain a new discovery path;
no client link or company directory is invented. Generic labels avoid copying
stored folder names. Loading, empty, failure and invalid-lineage states are explicit.

## Acceptance and release ledger

| Date       | State    | Observation / remaining gate                                                                                                                          |
| ---------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-11-08 | Prepared | Typed staff-only endpoint, minimal CompanyTab import/render, synthetic backend and UI tests; coordinator must independently accept, gate and release. |

Bites: the staff Company tab renders direct Drive links with their retained
provenance from the new endpoint; denied roles receive 403 before a database read.
This is local test evidence only until the coordinator verifies deployment.

Tests cover staff allow/external deny, duplicate and malformed sources, JSON
custom fields, missing and invalid merge targets, historical IDs, direct links,
empty/error/loading states, late responses when switching companies, and absence
of mutation controls/calls. Fixtures contain no production IDs or client data.

Local verification: 132 backend tests (source index, company router and typed
bodies), 113 frontend tests across four files (source component, CompanyTab,
CompanyTab validity and CRM API), TypeScript, Ruff and production-file ESLint pass.
Frontend dependencies were installed with `npm ci --prefer-offline --ignore-scripts`;
the tracked lockfile was unchanged. No database or Drive writes were performed.

Integration note: another open CompanyTab change (PR 27) may overlap the nearby
identity section. This concern adds one import and one component render only;
resolve against that change without replacing its existing content.
