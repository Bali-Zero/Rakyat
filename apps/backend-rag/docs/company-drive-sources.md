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
- Merge metadata is display-only. Accept a positive integer or its canonical
  ASCII decimal string, within the PostgreSQL integer range, distinct from the
  source ID and pointing to an existing terminal record. Normalize to an integer
  before querying or responding. Booleans, floats, signs, whitespace, leading
  zeroes, names, overflow, missing targets, self-links, cycles and multi-hop
  chains are invalid. Chains are intentionally not traversed.
- Terminal records expose up to 100 directly incoming historical record IDs,
  ordered by ID. Reverse lookup compares explicit JSON numbers and strings
  against one integer-typed parameter; it revalidates every returned value
  because JSONB numeric equality can equate integral floats and integers.

The canonical-string compatibility fixes a live historical merge reference
without rewriting stored data. Synthetic tests cover forward and reverse paths,
including a PostgreSQL TEMP-table proof in an isolated test database; no real
company identifiers or historical rows are included in those fixtures.

## Source/proxy boundary

Metadata is information, never an access grant. Links open Google Drive directly
with the viewer's existing Google permissions. No folder listing, proxy download,
permission change, document registration, synchronization, import or ingestion
is added. Historical records do not contribute documents or folders to a successor.
The existing primary-only sync action and document proxy remain unchanged.

The component appears only when the selected company comes from an explicitly
active client link. Both profile `status` and `/by-client` `link_status` are
supported; missing or conflicting statuses fail closed. A by-name fallback never
requests these sources. Changing the client, selected link or its status hides
stale sources immediately. No client link or company directory is invented.
Generic labels avoid copying stored folder names. Loading, empty, failure and invalid-lineage states are explicit.

## Acceptance and release ledger

| Date       | State    | Observation / remaining gate                                                                                                                                |
| ---------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-11-08 | Prepared | Typed staff-only endpoint, active-link CompanyTab selection guard, synthetic backend and UI tests; coordinator must independently accept, gate and release. |
| 2026-11-08 | Prepared | Canonical decimal-string lineage compatibility; synthetic forward/reverse and PostgreSQL TEMP proof; release owner verifies deployment.                     |

Bites: the staff Company tab renders direct Drive links with their retained
provenance from the new endpoint; denied roles receive 403 before a database read.
This is local test evidence only until the coordinator verifies deployment.

Tests cover staff allow/external deny, duplicate and malformed sources, JSON
custom fields, missing and invalid merge targets, historical IDs, direct links,
empty/error/loading states, late responses when switching companies, and absence
of mutation controls/calls. Fixtures contain no production IDs or client data.

Original source-index verification: 55 backend/compliance checks, 122 frontend tests
across four files (source component, CompanyTab, CompanyTab validity and CRM API),
TypeScript and the scoped secret scan pass. Nine attribution cases cover fallback,
missing/conflicting status, exact selected ownership and selection changes; eight
of these cases failed before the correction. Earlier broader router checks remain
recorded separately.
Frontend dependencies were installed with `npm ci --prefer-offline --ignore-scripts`;
the tracked lockfile was unchanged. No production database or Drive writes were
performed; the lineage compatibility proof uses disposable PostgreSQL TEMP rows.

Integration note: another open CompanyTab change (PR 27) may overlap the nearby
identity section. Preserve the source component plus its active-link selection
state, semantic key, effect dependency and render guard alongside existing content.
