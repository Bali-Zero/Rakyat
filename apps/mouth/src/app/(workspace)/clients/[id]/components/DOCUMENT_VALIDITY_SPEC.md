# documentValidity(doc): one validity model for the kita client card — spec v2

Status: **proposed, gated before code** (orchestrator ruling 2026-11-05). The
implementation lands in the same PR after this spec's gate; the final head gets its own
gate.

v1 (head 559f5444) failed its spec gate on ten points. v2 closes them by one principle:
**the card and the backend attention ledger give one answer.**
- Liveness is the backend predicate character for character.
- Expiry day 0 is expired, as in the ledger.
- Every void document stays visible, in one place.
- There is one clock with one refresh policy.
- One precedence decides every pill, label and tone.
- The pickers keep their comparators.

## Problem (measured on Rakyat main d8ff719b)

The card decides document validity in about 40 places. It uses three clocks, and no
surface reads `status` when deciding validity.

- **Pickers.** `pickCurrentPermitFor` (`pickCurrentVisa.ts:83-89`) and
  `pickCurrentPassport` (`utils.ts:294-311`) filter only on `deleted_at` and
  `is_archived`. A `rejected` document can become the "Current" permit, with a green pill,
  a countdown, a Valid ledger row and "Start Renewal".
- **Documents tab.** `DocumentsTab.tsx:70-73` shows "Valid" for any `verified` document,
  whatever its date. Next to "Rejected", the Expires cell still counts down.
- **Tax.** `sptFilings` (`tax-deadlines.ts:217`) and `coretax.ts:37` count a rejected
  document as filed or on file.
- **Three clocks:**
  - WITA calendar days (`daysUntil`, `overview-brief.ts:66`);
  - `Math.ceil` against `Date.now()`, which parses a date-only string as UTC midnight
    (`ImmigrationTab.tsx:48`, `DocumentsTab.tsx:93`, `FamilyTab.tsx:210`, `FamilyTab.tsx:233`, `ProcessTab.tsx:370`);
  - `Math.round` on timestamps (`ValidityTrack.tsx:27-35`);
  - plus 30-day months (`utils.ts:244-247`).
- **Day 0.** The backend ledger calls expiry day 0 expired (`client_attention.py:247`,
  `:291`, `:330`; SQL `alert_color` `expiry_date <= CURRENT_DATE`, `crm_enhanced.py:1152`).
  The card calls it "Expires today" and still valid. The local ledger row
  (`attention.ts:255`) calls it Expiring.
- **Server enums on NULL.** The family enums `visa_alert` and `passport_alert`
  (`crm_enhanced.py:1572-1583`) fall to `ELSE 'green'` when their date is NULL. A member
  with no `visa_expiry` therefore gets "Valid", even beside an expired fallback document
  date (`FamilyTab.tsx:192`).

Production data, 2026-11-05:
- Client document statuses reaching the card: `active`, `verified`, `received`, `pending`,
  `google_drive`. None is `rejected` today, so the rejected defect is latent.
- The profile SELECT drops `is_archived` rows and team-deleted rows (`crm_enhanced.py:1160-1161`).
- Client-deleted rows (`deleted_at` set, restorable for 30 days) do reach the card.
- Company documents reach the card only with `status = 'active'`.

## The model

```ts
type ValidityState = "valid" | "expiring" | "expired" | "undated" | "void";

interface DocumentValidity {
  live: boolean;           // held: may be current, filed, on file, counted, renewed
  state: ValidityState;    // "void" iff !live
  voidReason: "removed" | "archived" | "rejected" | null; // non-null iff state === "void"
  expiry: string | null;   // WITA calendar date "YYYY-MM-DD", or null
  daysLeft: number | null; // WITA calendar days today -> expiry; null iff expiry is null
  markedExpired: boolean;  // live, status "expired", and daysLeft > 0 (Rule D downgrade)
}

documentValidity(doc, now: number): DocumentValidity
dateValidity(date: unknown, now: number): Pick<DocumentValidity, "state" | "expiry" | "daysLeft">
```

### Rule L: liveness, identical to the backend `_usable` (`client_attention.py:214-219`)

The backend reads:

```py
doc.get("deleted_at") is None and not doc.get("is_archived")
and str(doc.get("status") or "").lower() not in {"rejected", "deleted"}
```

The card applies the same three tests, in this order. The first that fires gives the void
reason.

1. **Removed.** `deleted_at` is anything other than `null` or `undefined`, the empty
   string included. Python `is None` treats `""` as set.
2. **Archived.** `Boolean(is_archived)` is true. For the column's possible types
   (boolean, null, number, string), JS and Python truthiness agree.
3. **Rejected.** `String(status ?? "").toLowerCase()` is exactly `rejected`; or it is
   exactly `deleted`, which gives the reason `removed`.
   - **No trimming:** `" rejected "` stays live, as in the backend.
   - **No other status** voids. `archived`, `expired`, `pending`, NULL and any unknown
     string are live.

A void document is never:
- the current permit or passport;
- filed or on file;
- counted as held or as expiring;
- urgent;
- a renewal target.

It is never shown with a countdown or a positive label. It is always listed, with its
reason (see Visibility).

### Rule D: one date grammar, one clock, one day-0 rule

**Date grammar** for `expiry_date`, `issue_date`, `passport_expiry` and `visa_expiry`. A
value is a date only in one of two forms:
- a string of exactly `YYYY-MM-DD` that is a real calendar date;
- a string that starts with a real `YYYY-MM-DD` and carries a time with an explicit offset
  (`Z` or `±hh:mm`). It is reduced to its Asia/Makassar date.

Anything else gives `expiry = null`:
- an impossible date, even with a time part (`2026-02-30T00:00:00Z`), because `Date.parse`
  would roll it over;
- a time without an offset (its zone would be the browser's);
- a non-string;
- an empty string.

`daysUntil` and `calendarKey` (`overview-brief.ts:38-71`) are tightened to this grammar.
Every surface uses them.

**Days.** `daysLeft = dayNumber(expiry) − dayNumber(witaDateKey(now))`, in whole calendar
days.

**State of a live document:**

| Condition | State |
|---|---|
| `expiry` is null | `undated` |
| `daysLeft <= 0` | `expired`: day 0 is expired, as in the backend ledger |
| `1 <= daysLeft <= 60` (`EXPIRY_WARNING_DAYS`) | `expiring` |
| otherwise | `valid` |
| live, `status` (lower-cased) is `expired`, `daysLeft > 0` | `expired`, `markedExpired = true` |
| live, `status` is `expired`, `expiry` null | `expired` |

A status may downgrade a date, never upgrade it. `dateValidity` applies the same table to a
bare date. It has no liveness and no status.

**Wording ladder.** One shared function serves every date cell and countdown:

| State / days | Wording |
|---|---|
| expired, `daysLeft < 0` | "Expired N d ago" |
| expired, `daysLeft === 0` | "Expired today" (was "Expires today": the ledger already says "expired … 0 days ago") |
| `markedExpired` | "Marked expired" |
| expired, undated | "Expired" |
| `1..365` days | "⏰ N d left" |
| above 365 days | "Ny Nmo left" |
| undated | no countdown |

`spanValue` (`overview-brief.ts:81`) and `ExpiryText` take the state. A day-0 permit reads
"Expired" on the Overview cards.

**Clock.**
- One `now` per card. `ClientDetailClient` owns it in a `WitaClock` context and ticks it
  every `CLOCK_TICK_MS` (60 s, the Overview's existing tick, `OverviewTab.tsx:55`). Every
  tab reads that context.
- Every memoised derivation that reads a date lists `witaDateKey(now)` among its
  dependencies, so counts and labels change at 00:00 WITA without a remount. That includes
  the Documents counts at `DocumentsTab.tsx:209-224`.
- No component in this folder calls `Date.now()`, `new Date()` for day arithmetic, or
  divides by `86400000`. The clock module and `overview-brief.ts` are the only exceptions.

**ValidityTrack contract.**
- Props: `issueDate`, `expiryDate` and `now`.
- `daysTotal = dayNumber(expiry) − dayNumber(issue)`.
- `daysRemaining = clamp(daysLeft, 0, daysTotal)`.
- `daysElapsed = daysTotal − daysRemaining`.
- Fill: `daysElapsed / daysTotal`.
- It is not rendered when either date is null under the grammar, or when
  `daysTotal <= 0`.
- Example: issue 2026-11-01, expiry 2026-11-06, 23:30 WITA on 2026-11-05: total 5,
  remaining 1, elapsed 4.

### Precedence: one order for every pill, label, tone and urgency flag on a document

1. **void.** Void-reason label ("Removed", "Archived", "Rejected"), neutral tone. No
   countdown, no urgency, no renewal.
2. **expired.** Expired wording from the ladder, warning tone, urgent.
3. **expiring.** Countdown, warning tone, urgent.
4. **valid, undated.** As each surface does today, with one limit: **a positive label**
   ("Valid", "Valid until", the `ok` tone) **requires state `valid` or `expiring`**. A
   `verified`, undated document reads "Verified", not "Valid".

**Server enums.** These are `alert_color` on documents, and `visa_alert` /
`passport_alert` on family members.
- They may add urgency or downgrade a label. They never upgrade one.
- A family enum is read only when its source field is a date under the grammar, because
  the SQL turns NULL into `green`.
- When both an enum and the model speak, the worse wins. The order is expired, red,
  yellow, green.
- `alert_color` stays an urgency signal on its own (`ImmigrationTab.tsx:81-90`), as today.

### Windows stay per surface, on `daysLeft`

Each business window keeps its value and reads `daysLeft`:

| Window | Rule |
|---|---|
| Start Renewal | `daysLeft !== null && daysLeft <= 90`, expired included |
| Documents "expiring soon" / urgency | `daysLeft !== null && daysLeft <= 90` |
| Immigration urgency | `alert_color ∈ {red, yellow, expired}`, or `daysLeft !== null && daysLeft <= 90` |
| Passport expired | `daysLeft <= 0` |
| Passport critical | `daysLeft < 270` |
| Passport warning | `daysLeft < 420` |
| Passport label | `${Math.floor(daysLeft / 30)} months` |
| Family passport fallback, enum unreadable | `daysLeft <= 180` |
| Local ledger Visa row "Expiring" | `daysLeft <= 60` |

All of these apply only to live documents with a non-null `daysLeft`. An undated document
is never renewable, never "expiring soon" and never urgent by date. It can still be urgent
by `alert_color`, which the server only sets from a date.

### Visibility: every void document is listed exactly once

- The Documents tab lists today's general documents, **plus every void document the card
  receives, whatever its kind**: passport, immigration, family, PMA, tax.
- They sit in one filter group, "Removed or rejected", placed after the categories. Each
  row shows:
  - its type and void reason;
  - its date, with no countdown;
  - today's "Removed by the client on …" line where `deleted_at` is set;
  - its existing file action.
- A void general document moves from its category into that group.
- Domain surfaces list and count only live documents: Immigration, Family, the Passport
  and Visa cards, the Company vault and Tax.
- A domain surface that drops a void document of its own kind shows one signpost line:
  "N removed or rejected files are in Documents". The Documents tab opens on that group.
  Signposts appear on the Immigration tab, the Family tab, the Passport card and the
  Company document vault.
- **Count classes.**
  - **Listing counts** label a list the user sees, and equal its row count, void rows
    included:
    - the "Documents (N)" tab badge (`ClientDetailClient.tsx:1019`);
    - the Documents strip count;
    - each filter group's total.
  - **Holding counts** are every count in a sentence about possession: "documents on
    file", "Family permits are in the Family tab (N)", "expiring soon", filed and on-file
    counts. They count live documents only.

## Surface table

Every row moves to the model in the implementing PR. "Unchanged" means unchanged apart
from Rule L, Rule D and the precedence.

| # | Surface | Today | After |
|---|---|---|---|
| 1 | `pickCurrentPermitFor` / `pickCurrentVisa` (`pickCurrentVisa.ts:79-125`) | Candidate filter: `deleted_at`, `is_archived` | The filter becomes `documentValidity(doc).live`. **The comparator is unchanged:** (a) farthest stored `expiry_date`, ties to the higher id; (b) an undated candidate (newest `created_at`, then id) wins only when uploaded after the dated winner; (c) when the winner is not a stay-permit document, the live stay-permit document of the same permit (`isSamePermit`) wins, by issue date, upload date, id. `pickCurrentVisa.guard.test.ts` passes unchanged. |
| 2 | `pickCurrentPassport` (`utils.ts:294-311`) | Same filter; newest `created_at`, then id | Filter becomes `live`; comparator unchanged. |
| 3 | VisaCard Expires, `ExpiryText`, runway, empty state | Current permit `expiry_date`; `spanValue` sign | `documentValidity(current)`: state, ladder, `daysLeft`. With no live permit: "No valid visa or KITAS on file" if a void permit exists (plus the signpost), else today's "No visa or KITAS on file". |
| 4 | PassportCard Expires, colour and alert line | `client.passport_expiry`; 30-day months | `dateValidity(client.passport_expiry)` drives the text, colour and alert, using the passport windows. The scan shown is the live pick. |
| 5 | PassportCard void passports | Removed ones are hidden; a rejected one can be current | Never picked. When void passport documents of the client exist: signpost (they are listed in Documents). |
| 6 | overview-brief `permitFact` / `passportFact` | WITA, current pick | Inputs are the live picks; the state comes from the model (day 0 is expired). |
| 7 | Local ledger Visa row (`attention.ts:239-276`) | `days < 0` expired; `<= 60` Expiring | Model state: expired at `daysLeft <= 0` ("expired 0 days ago" on day 0), Expiring at 1..60. A void-only client has no local Visa row. |
| 8 | Server attention rows and counters | Server | Unchanged: the server already applies Rule L and day 0. |
| 9 | Header permit line | Current pick | The live pick from row 1. |
| 10 | Immigration current-permit panel: numerals, Expires, label | `Math.ceil`; "Expires today" | `documentValidity(current)`: `daysLeft` and the ladder. |
| 11 | `ValidityTrack` | Timestamps, `Math.round`, `Date.now()` | The contract above, with `now` from the clock context. |
| 12 | Immigration `isUrgent` / `expiryTone` | `alert_color` or ceil ≤ 90 | `live && (alert_color ∈ {red, yellow, expired} \|\| (daysLeft !== null && daysLeft <= 90))`. |
| 13 | Start Renewal (panel and history rows) | Renewable type; dated; ceil ≤ 90 or past | Renewable type, `live`, `daysLeft !== null && daysLeft <= 90`. |
| 14 | Immigration list, buckets and sort | All statuses | Live documents only, with today's buckets and sort. The "current" bucket is the live pick. Void ones get the signpost. |
| 15 | History status pill | "Verified" / "Current" / "Completed" | As today for live rows. "Current" marks the pick, not validity. |
| 16 | History "Valid until" / "Ended" (`ImmigrationTab.tsx:350`) | Sign of ceil days | Both dates known: the period, as today. Expiry only: `valid`/`expiring` gives "Valid until <date>"; `expired` with `daysLeft <= 0` gives "Ended <date>"; `markedExpired` gives "Marked expired · <date>". Undated: today's issue/upload text. |
| 17 | MERP chips | `expiry_date` | Live only; `expiry` from the model. |
| 18 | Immigration empty state | Any immigration document | "No immigration documents yet" when no live one exists. The signpost shows below it when void ones exist. |
| 19 | Family-permit signpost (`ImmigrationTab.tsx:281`, `:878`) | All family immigration documents | Holding count: live family immigration documents only. |
| 20 | Documents tab pill (`DocumentsTab.tsx:67-75`) | `deleted_at` → Removed; verified → Valid; received → Processing; rejected → Rejected | Precedence. Void gives its reason. Live expired gives "Expired". `verified` gives "Valid" in `valid`/`expiring` and "Verified" when undated. `received` gives "Processing". |
| 21 | Documents Expires cell (`DocumentsTab.tsx:87-100`) | ceil countdown, any status | Live: the ladder, urgent per the 90-day window. Void: the date only. |
| 22 | Documents "expiring soon" totals (`DocumentsTab.tsx:209-224`) | Any status | Holding: `live && daysLeft !== null && daysLeft <= 90`. Memo deps include the WITA day key. |
| 23 | Documents list, groups, strip count, badge (`ClientDetailClient.tsx:523`, `:1019`) | General documents only | General live documents in their categories, plus the "Removed or rejected" group (every void document). Strip count, group totals and the badge are listing counts. Order within a group: as today. |
| 24 | Documents empty state | No general documents | "No documents on file yet." only when the tab has no rows at all. |
| 25 | FamilyTab member permit and passport: date, countdown, pill, urgency (`FamilyTab.tsx:170-260`) | Server enum; ceil; first document via `.find()` | **Date:** `member.visa_expiry`, else the live pick `pickCurrentPermitFor(docs, member.id)`. The passport is chosen likewise from that member's live passport documents. **Countdown:** `dateValidity` and the ladder. **Pill:** the worse of the enum (only when `member.visa_expiry` is a date) and the model state. Model-only labels: expired gives "Expired"; expiring gives "Expiring soon"; valid gives "Valid"; with no date, today's "Visa on file" / "No visa". **Passport urgency:** the worse of the enum (only when `member.passport_expiry` is a date) and the model; the fallback is `daysLeft <= 180`. |
| 26 | Family tab void documents | `.find()` may show one | Not shown on members; signpost to Documents. |
| 27 | Overview family rows | WITA, server enums | `dateValidity`, plus the enum rule of row 25. |
| 28 | ProcessTab practice chip | ceil on `practice.expiry_date` | `dateValidity(practice.expiry_date)` and the ladder. This is a practice, not a document: no liveness. |
| 29 | Tax SPT "Filed" (`sptFilings`) | Excludes only archived and removed | A filing requires `live`. |
| 30 | Coretax / EFIN on file (`coretax.ts:37`) | Excludes only archived and removed | Requires `live`. |
| 31 | `companyDocumentToClientDocument` | Forces `is_archived: false`, `deleted_at: null` | Maps `status` as received. Rule L reads it. (The server sends only `active` company documents: no visible change today.) |
| 32 | Company vault slots | Drive id present | A slot is filled by a live document. A void PMA document gets the vault signpost and is listed in Documents. |
| 33 | "N documents on file" (`CompanyTab.tsx:584`, `CompanyIdentity.tsx:127`) | Every PMA and company document | Holding count: live documents only. |
| 34 | AiSummaryCard (server L1 summary) | Server | Unchanged, out of scope: it does not read documents. |

## Not in scope

- Server-side `alert_color`, `visa_alert`, `passport_alert` and the attention ledger. The
  card reads them under the enum rule above; their SQL is not changed.
- Practice statuses.
- The status union. `crm.types.ts:389` gains `active` and `google_drive`, to match
  production. This is a type change only.

## Test obligations for the implementing lane

1. **Rule L truth table**, mirrored from `_usable`. Each row asserts `live` and the reason:
   - `deleted_at` null, undefined, `""` and a timestamp;
   - `is_archived` true, false, null and undefined;
   - status `rejected`, `REJECTED`, `" rejected "` (live), `deleted`, `archived` (live),
     `expired` (live), null, an unknown string.
2. **Rule D:**
   - the grammar: date-only; offset instant across 00:00 WITA; naive time (null);
     `2026-02-30` with and without a time (null); non-string;
   - the boundaries `daysLeft` −1/0/1 (expired/expired/expiring) and 60/61;
   - `markedExpired` with a future date, and status `expired` with no date.
3. **Precedence:**
   - a void document with a future date: reason label, no countdown, never urgent;
   - a `verified` document in each state: Valid, Valid, Expired, Verified;
   - a family member with NULL `visa_expiry`, enum `green` and an expired fallback document
     reads "Expired";
   - enum `expired` beside a model `valid` reads "Expired";
   - `alert_color: red` at +200 days stays urgent (row 12).
4. **Windows:** boundary pairs 0/1, 60/61, 90/91, 180/181, 269/270 and 419/420, each on the
   surface that uses it. An undated live document is never renewable, never "expiring
   soon", and gets no "Valid until".
5. **Pickers:** `pickCurrentVisa.guard.test.ts` passes unchanged. A rejected document with
   the farthest expiry is never picked, and its live twin is.
6. **Visibility and counts.** For one void fixture of each kind (passport, own permit,
   family permit, PMA, general, tax):
   - it appears exactly once, in the Documents "Removed or rejected" group;
   - it is absent from every domain list and holding count;
   - the right signpost shows;
   - the badge and strip counts equal the rows shown.
7. **One clock:**
   - fake timers cross 00:00 WITA with no remount; the Visa card, the Immigration panel,
     ValidityTrack, the Documents cell and totals, and the Family row all change together;
   - at 23:30 and 00:30 WITA, those surfaces show the same `daysLeft`;
   - a static test finds no `Date.now()` or `86400000` in this folder outside the clock
     module and `overview-brief.ts`.
8. **Ledger parity:** a day-0 permit and a day-0 passport read expired on the card, and
   the server ledger fixture says expired too.
9. Existing tests that pin old behaviour change in the same PR, each named in the PR body.
   Examples: `DocumentsTab.test.tsx` "Expires today", the Valid pill, `PassportCard`
   colour tests.
10. **Mutation:** one mutant per rule (L1, L2, L3, no-trim, day 0, the downgrade, the enum
    source rule, the positive-label rule, each picker filter, each holding count, the
    90-day window, and the ValidityTrack clamp). The lane reports killed/total and names
    any survivor with its reason.
11. Synthetic fixtures only.
