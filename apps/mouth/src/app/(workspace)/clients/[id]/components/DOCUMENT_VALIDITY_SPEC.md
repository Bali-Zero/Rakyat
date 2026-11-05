# documentValidity(doc): one validity model for the kita client card

Status: **proposed, gated before code** (orchestrator ruling 2026-11-05). The
implementation lands in the same PR after this spec's gate. The final head gets its own
gate.

## Problem (measured on Rakyat main d8ff719b)

The client card decides document validity in 31 places. It uses three clocks, and no
surface reads `status` when deciding validity.

- **The picker.** `pickCurrentVisa` (`pickCurrentVisa.ts:83-89`) and
  `pickCurrentPassport` (`utils.ts:294-311`) filter only on `deleted_at` and
  `is_archived`. A `rejected` or `expired` document can become the "Current" permit, with
  a green pill, a countdown, a Valid ledger row and a "Start Renewal" button.
- **The Documents tab.** `DocumentsTab.tsx:73` shows a "Rejected" pill, while the
  Expires cell beside it counts down as if the document were valid.
- **Tax.** `sptFilings` (`tax-deadlines.ts:217`) and `coretaxStatus` (`coretax.ts`) count
  a rejected document as filed or on file.
- **Three clocks:**
  - WITA calendar days, from `daysUntil` in `overview-brief.ts:66`;
  - `Math.ceil` against `Date.now()`, which parses a date-only string as UTC midnight,
    in `ImmigrationTab.tsx:47`, `DocumentsTab.tsx:88`, `FamilyTab.tsx:209` and
    `ProcessTab.tsx`;
  - 30-day months, in `getPassportValidityColor` (`utils.ts:224`).

  One card can show two answers for the same date.

The backend already has the rule that matters (`services/crm/client_attention.py:216-218`):

- a document counts only when `deleted_at` is NULL;
- and `is_archived` is false;
- and `status` is not `rejected` or `deleted`.

The card must use the same rule, so the attention ledger and the card never disagree.

Production data, 2026-11-05:
- Client document statuses reaching the card: `active`, `verified`, `received`,
  `pending`, `google_drive`. None is `rejected` today, so the rejected defect is latent.
- `status = 'deleted'` is filtered server-side (`admin_document_not_team_deleted_clause`).
- Client-deleted rows (`deleted_at` set, restorable for 30 days) do reach the card.
- Company documents reach the card only with `status = 'active'` (profile SELECT).

## The model

```ts
type ValidityState = "valid" | "expiring" | "expired" | "undated" | "void";

interface DocumentValidity {
  live: boolean;           // counts as held: can be current, filed, on file, counted
  state: ValidityState;
  voidReason: "removed" | "archived" | "rejected" | null; // set iff state === "void"
  expiry: string | null;   // YYYY-MM-DD (WITA calendar date) or null
  daysLeft: number | null; // WITA calendar days from today to expiry; null iff expiry null
}

documentValidity(doc, now: number): DocumentValidity
dateValidity(date: string | null | undefined, now: number): Omit<DocumentValidity, "live" | "voidReason">
```

### Rule L: liveness

Rule L mirrors the backend, and is checked in this order:

1. `deleted_at` is set, so the document is **void / removed**.
2. `is_archived === true`, or the status is `archived`, so it is **void / archived**.
3. The status (trimmed, lower-cased) is `rejected` or `deleted`, so it is
   **void / rejected**. `deleted` maps to `rejected`'s reason only as a safety net; the
   server already filters it.
4. Anything else is **live**: `active`, `verified`, `received`, `pending`,
   `google_drive`, `expired`, NULL, or an unknown string.

   An unknown status never voids a document. Only these statuses do, so the card and the
   backend ledger keep one rule.

A void document is never:
- the current permit or passport;
- filed or on file;
- counted in "expiring" totals;
- a renewal target.

It is still listed where every document is listed (the Documents tab and the history),
with its void reason.

### Rule D: date and clock

There is **one clock**: `daysUntil` from `overview-brief.ts`, in WITA calendar days.

- A date-only string is taken as written.
- An instant is converted to its `Asia/Makassar` date.
- An impossible date (`2026-02-30`) or an unparseable one gives `expiry = null`.

`state` for a live document:
- `expiry` null gives `undated`;
- `daysLeft < 0` gives `expired`;
- `0 <= daysLeft <= EXPIRY_WARNING_DAYS` (60) gives `expiring`. Expiry day 0 is
  "expires today" and still counts as held;
- otherwise `valid`.

A live document whose `status` is `expired` has state `expired` whatever its date. A
status may downgrade a date, never upgrade it.

`dateValidity` applies Rule D to a bare date. It covers the client and family fields that
are not documents: `client.passport_expiry`, `member.visa_expiry` and
`member.passport_expiry`.

### Windows stay per surface, on `daysLeft`

Business windows are not validity. Each window keeps its documented constant and reads
`daysLeft` from the model:
- Start Renewal: 90 days;
- Documents-tab urgency: 90 days;
- passport critical: under 9 months;
- passport warning: under 14 months, measured in days as 9×30 and 14×30, with the same
  numbers as today;
- the family passport fallback: 180 days.

The windows are listed in the table below. This keeps existing thresholds. Only the clock
and liveness become shared.

## Surface table: what each surface reads

The # numbers refer to the 2026-11-05 inventory. Every row moves to the model in the
implementing PR.

| # | Surface | Today | After |
|---|---|---|---|
| 1 | `pickCurrentVisa` / `pickCurrentPermitFor` | Filters `deleted_at` and `is_archived`; the farthest expiry wins | Candidates are permit documents with `live`. The farthest `expiry` wins. An undated live document wins only as today (newer `created_at` than the dated winner). A void document is never picked. |
| 1b | `pickCurrentPassport` | Same filters; the newest `created_at` | Candidates are passport documents with `live`; the newest `created_at` wins. |
| 2 | VisaCard Expires, `ExpiryText`, runway, empty state | Current permit `expiry_date` | Reads `documentValidity(current)`, so it shows `state` and `daysLeft`. With no live permit document, it shows "No valid visa or KITAS on file" if any void permit exists, and today's "No visa or KITAS on file" otherwise. |
| 3 | PassportCard Expires and alert line | `client.passport_expiry`, plus the 30-day-month colour | `dateValidity(client.passport_expiry)` drives both the text and the alert, so there is one clock. The thresholds are the days windows above. |
| 4 | overview-brief `permitFact` / `passportFact` | WITA, current pick | Unchanged clock. Inputs come from the live picks (rows 1 and 1b). |
| 5 | Local ledger Visa row (fallback when the server sends no permit item) | Current pick, WITA | Reads `documentValidity(current)` state and days. A void-only client shows no local Visa row, matching the server. |
| 6 | Server attention rows and counters | Server | Unchanged. The server already applies Rule L. |
| 7 | Header permit line | Current pick | Uses the live pick from row 1. |
| 8 | Immigration current-permit panel: numerals, Expires, ValidityTrack | `Math.ceil` | Reads `documentValidity(current)`; `daysLeft` replaces the ceil count. |
| 9 | Immigration `isUrgent` / `expiryTone` | `alert_color` or ceil ≤ 90 | `live && daysLeft !== null && daysLeft <= 90`. Server `alert_color` stays a fallback only when `expiry` is null. |
| 10 | Start Renewal (current panel and history rows) | Renewable type and ceil ≤ 90, or expired | Requires a renewable type, `live`, and `daysLeft <= 90` (expired included, as today). Never shown on a void document. |
| 11 | Immigration history Status pill | "Verified" / "Current" / "Completed" | A void document shows its void reason ("Rejected", "Removed", "Archived"). The rest stays as today, on the live pick. |
| 12 | History "Valid until" / "Ended" | ceil | `daysLeft >= 0` gives "Valid until"; otherwise "Ended". Void documents prefix their reason. |
| 13 | Immigration buckets and sort | All statuses | Unchanged listing; the "current" bucket uses the live pick. |
| 14 | MERP chips | `expiry_date` | Show `expiry`, plus the void reason when void. |
| 15 | Immigration empty state | Document count | Unchanged ("No immigration documents yet" counts every document). |
| 16 | Documents tab status pill | `verified` / `received` / `rejected` / removed | Unchanged labels, plus "Archived". The pill and the Expires cell come from the same model. |
| 17 | Documents tab Expires cell | ceil countdown, any status | A live document shows the countdown from `daysLeft`. A void document shows the date with no countdown and no urgency colour. |
| 18 | Documents tab "expiring soon" counts | ceil ≤ 90, any status | Count `live && daysLeft !== null && daysLeft <= 90` (expired live documents included, as today). |
| 19 | Documents tab order | Expiry | Unchanged (all documents, nearest expiry first). |
| 20 | Documents tab empty state | Count | Unchanged. |
| 21 | FamilyTab member visa and passport: date, countdown, pill | Server `visa_alert`, ceil, `.find()` of the first document | **Date:** `member.visa_expiry`, else the live pick for that member (`pickCurrentPermitFor(docs, member.id)`); the passport likewise from the live passport documents of that member. **Clock:** `dateValidity` for the countdown. **Pill:** keeps the server enum when present; with no enum, it uses the model state. |
| 22 | Overview family rows | WITA, server enums | `dateValidity`. Unchanged otherwise. |
| 23 | ProcessTab practice chip | ceil on `practice.expiry_date` | `dateValidity(practice.expiry_date)` for days. This is a practice, not a document, so liveness does not apply. |
| 24 | Tax SPT "Filed" (`sptFilings`) | Any status except archived or removed | A filing requires `live`. A rejected SPT is not a filing. |
| 25 | Tax server attention status | Server | Unchanged. |
| 26 | Coretax / EFIN "on file" | Archived or removed check only | Requires `live`. |
| 27 | `companyDocumentToClientDocument` | Forces `is_archived: false`, `deleted_at: null` | Maps `status` as is, and sets `is_archived` true for `archived`. The model's Rule L then applies. Today the server sends only `active` company documents, so there is no visible change. This removes the latent gap. |
| 28 | CompanyTab document vault slots | Drive id present | A slot is filled by a live document. A void document shows its reason in the slot. |
| 29 | CompanyTab document count | All `pma` documents | Unchanged (counts every document on file). |
| 30–31 | AiSummaryCard (server L1 summary) | Server | Unchanged, out of scope: it does not read documents. |

## Not in scope

- Server-side `alert_color`, `visa_alert` and the attention ledger. They are already on
  Rule L, or are server enums.
- Practice statuses.
- The status union. `crm.types.ts:389` gains `active`, `google_drive` and `archived` so
  the type matches production. That is a type change only, with no new behaviour beyond
  Rule L.

## Test obligations for the implementing lane

1. `documentValidity`:
   - each void reason and its precedence;
   - each live status, including an unknown string;
   - `status: "expired"` with a future date;
   - date-only and instant expiries across the WITA midnight;
   - an impossible date;
   - the day-0 and day-60 boundaries.
2. A guilt test per surface row that changes. A rejected document with a future expiry is:
   - never the current permit or passport;
   - never "Filed";
   - never "on file";
   - never counted as expiring;
   - never offered Start Renewal;
   - shown with no countdown in the Expires cell.

   A live twin of the same document keeps today's behaviour.
3. A clock-agreement test. For one date-only expiry evaluated at 23:30 and 00:30 WITA,
   the following show the same `daysLeft`:
   - the Visa card;
   - the Immigration panel;
   - the Documents-tab cell;
   - the Family row.
4. Existing tests that pin the old behaviour are updated in the same PR, each change
   named in the PR body. Examples: `DocumentsTab.test.tsx:126`, `pickCurrentVisa.guard`,
   `PassportCard.current.guard`.
5. Mutation, one per rule: L1, L2, L3 and D, the downgrade rule, and the live filter in
   each picker. The lane reports the killed/total count.
6. Synthetic fixtures only.
