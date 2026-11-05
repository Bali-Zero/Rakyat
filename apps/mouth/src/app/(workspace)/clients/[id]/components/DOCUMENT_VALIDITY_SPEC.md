# documentValidity(doc): one validity model for the kita client card, spec v3

Status: **proposed, gated before code** (orchestrator ruling 2026-11-05). The
implementation lands in the same PR after this spec's gate; the final head gets its own
gate.

## History

**v1** (head 559f5444) failed on ten points.

**v2** (head babfe768) closed them by one principle: the card and the backend attention
ledger give one answer. Its own gate then failed on nine points. Most of them showed that
v2 kept **two days and two parsers**:
- the browser's WITA day against the server's;
- a WITA-converting grammar against the backend's `value[:10]`;
- a status downgrade the backend does not make;
- no refresh of the server rows at midnight.

**v3 removes the second day and the second parser.**
- The card takes its day from the server (`as_of`) and its dates from the backend parser.
- The status downgrade is gone.
- The remaining v2 findings get explicit rules:
  - null numerals;
  - the date formatter;
  - the Drive browser;
  - the ProcessTab windows;
  - the family fallback;
  - the urgency formula.

## Problem (measured on Rakyat main d8ff719b)

The card decides document validity in about 40 places. It uses four clocks, and no
surface reads `status` when deciding validity.

- **Pickers.** `pickCurrentPermitFor` (`pickCurrentVisa.ts:83-89`) and
  `pickCurrentPassport` (`utils.ts:294-311`) filter only on `deleted_at` and
  `is_archived`. A `rejected` document can become the "Current" permit, with a green pill,
  a countdown, a Valid ledger row and "Start Renewal".
- **Documents tab.** `DocumentsTab.tsx:70-73` shows "Valid" for any `verified` document,
  whatever its date. Beside a "Rejected" pill, the Expires cell still counts down.
- **Tax.** `sptFilings` (`tax-deadlines.ts:217`) and `coretax.ts:37` count a rejected
  document as filed or on file.
- **Four clocks:**
  - WITA calendar days (`daysUntil`, `overview-brief.ts:66`);
  - `Math.ceil` against `Date.now()` with a UTC-midnight parse (`ImmigrationTab.tsx:48`,
    `DocumentsTab.tsx:93`, `FamilyTab.tsx:210`, `FamilyTab.tsx:233`, `ProcessTab.tsx:370`);
  - `Math.round` on timestamps (`ValidityTrack.tsx:27-35`);
  - 30-day months (`utils.ts:244-247`).
- **Day 0.** The backend ledger calls expiry day 0 expired (`client_attention.py:247`,
  `:291`, `:330`). The card calls it "Expires today", still valid. The local ledger row
  (`attention.ts:255`) calls it Expiring.
- **NULL enums.** The family enums (`crm_enhanced.py:1572-1583`) fall to `ELSE 'green'`
  when their date is NULL.
- **Day mismatch.** The server computes the ledger for its own WITA day at fetch time
  (`client_attention.py:93-94`). The card never refreshes those rows at midnight
  (`useClientDetail.ts:61-68`: no interval).
- **Formatter.** Dates are formatted in the browser's timezone (`ClientDetailClient.tsx:460-468`).
  In a browser west of UTC, a date-only expiry shows the previous day.

Production data, 2026-11-05:
- Client document statuses reaching the card: `active`, `verified`, `received`, `pending`,
  `google_drive`. None is `rejected` or `expired` today.
- The profile SELECT drops `is_archived` rows and team-deleted rows (`crm_enhanced.py:1160-1161`).
- Client-deleted rows (`deleted_at` set) reach the card.
- Company documents arrive only with `status = 'active'`.
- `expiry_date`, `issue_date`, `passport_expiry` and `visa_expiry` are DATE columns. The
  API sends them as `YYYY-MM-DD` or null.

## The day: `as_of`, one per card, from the server

**Backend (the only backend change).** `get_client_profile` (`crm_enhanced.py`) computes
`today = today_wita()` once. It passes `today=today` to `build_client_attention`, and adds
`"as_of": today.isoformat()` to the profile response. That happens on both branches:
attention built, and attention failed. The response has no `response_model`, so the
OpenAPI schema does not change. `crm.types.ts` gains `as_of?: string`.

**Card.**
- **The card's day** is the `as_of` of the profile payload it is showing.
  - It is the day of every derivation: local rows, countdowns, counts, ValidityTrack.
  - It is also the day the server used for the attention rows and counters in that same
    payload, so the two agree **by construction**.
  - The browser clock never sets the day.
- **The browser clock only asks for fresh data.** A `WitaClock` in `ClientDetailClient`
  compares `witaDateKey(Date.now())` with `as_of`. It checks every 60 s
  (`CLOCK_TICK_MS`), on window focus, and once on a timer set to the next 00:00 WITA.
  When the two differ, it invalidates `clientDetailQueryKey(clientId)`, at most once per
  tick.
  - Until the new payload lands, the card keeps showing the old payload, with its own
    `as_of` everywhere.
  - When the new payload lands, server rows and local derivations change in the same
    render.
- **Fallback.** When `as_of` is absent (an older backend), the card's day is
  `witaDateKey(Date.now())` at the time the payload was received. Every surface still
  uses that single value.
- **Memos.** Every memoised derivation that reads a date lists the card's day among its
  dependencies. No component in this folder calls `Date.now()` or `new Date()` for day
  arithmetic, or divides by `86400000`. The only exceptions are the `WitaClock` module and
  `overview-brief.ts`.
- **Known residue, out of this spec.** The document `alert_color` and the family enums
  are computed in SQL against the database's `CURRENT_DATE`. They are used only under the
  enum rule below, which can add urgency and never removes it. Their day is not the card's
  day.

## The model

```ts
type ValidityState = "valid" | "expiring" | "expired" | "undated" | "void";

interface DocumentValidity {
  live: boolean;           // held: may be current, filed, on file, counted, renewed
  state: ValidityState;    // "void" iff !live
  voidReason: "removed" | "archived" | "rejected" | null; // non-null iff state === "void"
  expiry: string | null;   // "YYYY-MM-DD" under the date grammar, or null
  daysLeft: number | null; // dayNumber(expiry) - dayNumber(day); null iff expiry is null
}

documentValidity(doc, day: string): DocumentValidity
dateValidity(value: unknown, day: string): Pick<DocumentValidity, "state" | "expiry" | "daysLeft">
```

### Rule L: liveness, identical to the backend `_usable` (`client_attention.py:214-219`)

```py
doc.get("deleted_at") is None and not doc.get("is_archived")
and str(doc.get("status") or "").lower() not in {"rejected", "deleted"}
```

The card applies the same tests in this order. The first one that fires gives the
reason.

1. **removed:** `deleted_at` is anything other than `null`/`undefined`. `""` counts as
   set.
2. **archived:** `Boolean(is_archived)`.
3. **rejected:** `String(status ?? "").toLowerCase()` is exactly `rejected`.
   - When it is exactly `deleted`, the reason is `removed`.
   - There is no trimming, and no other status voids: `archived`, `expired`, `pending`,
     NULL and unknown strings are all live.

A void document is never any of these:
- current;
- filed or on file;
- counted as held or expiring;
- urgent;
- renewable;
- shown with a countdown or a positive label.

It is always listed (see Visibility).

### Rule D: the backend's date parser, the server's day, day 0

**Date grammar (`_as_date` for strings, `client_attention.py:97-108`).**
- A value is a date when it is a string whose first 10 characters are an ISO
  `YYYY-MM-DD` real calendar date. That date is used as written, with no timezone
  conversion, whatever follows it.
- Anything else is null:
  - a non-string;
  - an empty string;
  - an impossible date;
  - a basic-format or week-format ISO string.
- The backend's `fromisoformat` would accept the last two, but a DATE column cannot
  produce them. For every value the API sends for these columns, card and backend read
  the same date.
- `calendarKey`/`daysUntil` in `overview-brief.ts` are rewritten to this grammar and take
  the card's day instead of `now`.

**State of a live document.** There is no status downgrade: the backend makes none, and
no row carries `status = 'expired'` today.

| Condition | State |
|---|---|
| `expiry` null | `undated` |
| `daysLeft <= 0` | `expired`. Day 0 is expired, as in the backend ledger. |
| `1 <= daysLeft <= 60` (`EXPIRY_WARNING_DAYS`) | `expiring` |
| otherwise | `valid` |

`dateValidity` applies the same table to a bare value. It has no liveness.

**Wording ladder.** One shared function:

| Input | Wording |
|---|---|
| `daysLeft < 0` | "Expired N d ago" |
| `daysLeft === 0` | "Expired today". The classification is fixed; the exact day-0 wording is with the owner, and a change re-earns the gate. |
| `1..365` | "⏰ N d left" |
| `> 365` | "Ny Nmo left" |
| undated | no countdown |

`spanValue` and `ExpiryText` take the state, so a day-0 permit reads "Expired".

**Formatter.** Every visible date of these fields goes through
`formatCalendarDate(expiry)`:
- `expiry_date`, `issue_date`, `client.passport_expiry`, `member.visa_expiry`,
  `member.passport_expiry`, `practice.expiry_date`.
- It formats the parsed `YYYY-MM-DD` with `timeZone: "UTC"` on `Date.UTC(y, m-1, d)`, so
  the day shown never depends on the browser's timezone.
- A null date renders as "—".
- The SSR placeholder behaviour of `formatDate` (`"..."` before mount) is kept.

**ValidityTrack contract.**
- Props: `issueDate`, `expiryDate` and `day`.
- `daysTotal = dayNumber(expiry) − dayNumber(issue)`.
- `daysRemaining = clamp(daysLeft, 0, daysTotal)`.
- `daysElapsed = daysTotal − daysRemaining`.
- The fill is `daysElapsed / daysTotal`.
- It is not rendered when either date is null or when `daysTotal <= 0`.

### Precedence: one order for every pill, label, tone and urgency flag

1. **void:** the reason label ("Removed", "Archived" or "Rejected"), a neutral tone, no
   countdown, not urgent, not renewable.
2. **expired:** the ladder wording, warning tone, urgent.
3. **expiring:** the countdown, warning tone, urgent.
4. **valid, undated:** as each surface shows them today, with one limit. **A positive
   label ("Valid", "Valid until", the `ok` tone) requires state `valid` or `expiring`.**
   A `verified` document with no date reads "Verified".

**Urgency formula** (every document surface that marks urgency):

```ts
urgent = live && (state === "expired" || state === "expiring" ||
                  (daysLeft !== null && daysLeft <= window) ||
                  enumUrgent)
```

- `window` is the surface's window (below).
- `enumUrgent` is `alert_color ∈ {red, yellow, expired}` where the surface reads
  `alert_color`.

**Enum rule.** This covers `alert_color`, `visa_alert` and `passport_alert`.
- An enum may add urgency or turn a label worse. It never turns one better.
- A family enum is read only when its source field (`member.visa_expiry` or
  `member.passport_expiry`) is a date under the grammar.
- When both an enum and the model speak, the worse wins. The order is expired, red,
  yellow, green. A model `expired` counts as `expired`; `expiring` counts as `red`;
  `valid` counts as `green`.

### Windows per surface (each on `daysLeft`, live documents, `daysLeft !== null`)

| Window | Value |
|---|---|
| Start Renewal | `daysLeft <= 90` (expired included) |
| Documents "expiring soon" / urgency | `daysLeft <= 90` |
| Immigration urgency | `daysLeft <= 90`, plus `alert_color` |
| Passport | expired `<= 0`; critical `< 270`; warning `< 420`; label `Math.floor(daysLeft / 30)` months |
| Family passport, enum unreadable | urgent at `<= 180` |
| Local ledger Visa row | "Expiring" at `1..60` |

An undated document is never renewable, never "expiring soon", and never urgent by date.

### Family: one shared result

`familyPermit(member, documents, day)` and `familyPassport(member, documents, day)`
return the following:
- the **date:** the member's own field when it is a date under the grammar; else the
  live pick (`pickCurrentPermitFor(documents, member.id)`, or the newest live passport
  document of that member) and its `expiry`;
- the **source:** `field`, `document` or `none`;
- the **validity:** `dateValidity` of that date;
- the **pill:**
  - with source `field`: the worse of the enum and the model;
  - with source `document`: the model alone (expired: "Expired"; expiring: "Expiring
    soon"; valid: "Valid");
  - with source `none`: today's "Visa on file" / "No visa".

Both the FamilyTab rows (`FamilyTab.tsx:170-260`, `:477-492`) and the Overview family rows
(`OverviewTab.tsx:455-466`) render from this one result. Passport urgency is the worse of
the enum (source `field`) and the model, with the 180-day fallback.

### Visibility: every void CRM document is listed exactly once

- The Documents tab lists today's general documents plus **every void document in the
  profile**, of whatever kind: passport, immigration, family, PMA or tax.
- The void documents sit in one filter group, "Removed or rejected", placed after the
  categories. A void general document moves into that group.
- Each row shows:
  - type, reason and the date (no countdown);
  - today's "Removed by the client on …" line;
  - its file action.
- Domain surfaces list and count live documents only: Immigration, Family, the
  Passport/Visa cards, the Company vault and Tax.
- Immigration, Family, the Passport card and the Company vault each show one signpost
  when they drop a void document of their kind: "N removed or rejected files are in
  Documents". It opens that group.
- **Out of the promise:** `FilesBrowserPanel` (`ClientDetailClient.tsx:1168-1169`). It
  lists the client's Drive folder, not CRM documents, so a file may appear there whatever
  its CRM state. Its heading stays "Drive folder" and it is not counted.
- **Listing counts** equal the rows of the list they label, void rows included:
  - the "Documents (N)" badge (`ClientDetailClient.tsx:1019`);
  - the strip count;
  - the group totals.
- **Holding counts** count live documents only. These are the possession sentences:
  - "documents on file";
  - "Family permits are in the Family tab (N)";
  - "expiring soon";
  - filed and on-file counts.

## Surface table

"Unchanged" means unchanged apart from Rule L, Rule D, the day, the formatter and the
precedence.

| # | Surface | Today | After |
|---|---|---|---|
| 1 | `pickCurrentPermitFor` / `pickCurrentVisa` (`pickCurrentVisa.ts:79-125`) | Filter on `deleted_at`, `is_archived` | The filter becomes `live`. **Comparator unchanged:** farthest stored `expiry_date`, ties to the higher id. An undated candidate (newest `created_at`, then id) wins only when uploaded after the dated winner. A non-stay winner yields to the live stay-permit document of the same permit, by issue date, then upload, then id. `pickCurrentVisa.guard.test.ts` passes unchanged. |
| 2 | `pickCurrentPassport` (`utils.ts:294-311`) | Same filter; newest upload | Filter becomes `live`; comparator unchanged. |
| 3 | VisaCard Expires, `ExpiryText`, runway, empty state | `spanValue` sign | Model state and ladder. No live permit: "No valid visa or KITAS on file" plus the signpost when a void permit exists, otherwise today's text. |
| 4 | PassportCard Expires, colour, alert | 30-day months | `dateValidity(client.passport_expiry, day)` and the passport windows. The scan is the live pick. |
| 5 | PassportCard void passports | Removed: hidden; rejected: may be current | Never picked. Signpost. |
| 6 | overview-brief `permitFact` / `passportFact` | WITA browser day | Live picks, model state, the card's day. |
| 7 | Local ledger Visa row (`attention.ts:239-276`) | `< 0` expired | Model state: expired at `<= 0` ("expired 0 days ago"), Expiring at 1..60. A void-only client has none. |
| 8 | Server attention rows and counters (`attention.ts:105-113`, `:292-297`) | Server, as fetched | Rendered as sent. They are computed for `as_of`, the card's day, and refreshed by the `WitaClock` refetch. |
| 9 | Header permit line | Current pick | Live pick. |
| 10 | Immigration panel numerals (`ImmigrationTab.tsx:622-648`) | `Math.max(0, -daysUntil(issue))`, `Math.abs(daysLeft)` | **"Days on this permit":** only when `issue_date` is a date; `max(0, dayNumber(day) − dayNumber(issue))`. **Expiry numeral:** only when `daysLeft !== null`. In state `expired` it reads `−daysLeft` with "Days past expiry" (0 on day 0); otherwise `daysLeft` with "Days to expiry". A null date shows no numeral. |
| 11 | `ValidityTrack` | Timestamps, `Math.round` | The contract above. |
| 12 | Immigration `isUrgent` / `expiryTone` (`ImmigrationTab.tsx:81-90`) | `alert_color`, or ceil ≤ 90 | The urgency formula with window 90 and `enumUrgent` from `alert_color`. |
| 13 | Start Renewal | Renewable type; dated; ≤ 90 or past | Renewable type, `live`, `daysLeft !== null && daysLeft <= 90`. |
| 14 | Immigration list, buckets, sort | All statuses | Live only, today's buckets and sort, plus the signpost. |
| 15 | History status pill | "Verified" / "Current" / "Completed" | As today on live rows. "Current" marks the pick. |
| 16 | History date text (`ImmigrationTab.tsx:350`) | Sign of ceil days; raw `formatDate` | Both dates: the period via the formatter. Expiry only: "Valid until <date>" in `valid`/`expiring`, "Ended <date>" in `expired`. Undated: today's issue/upload text. |
| 17 | MERP chips | `expiry_date` | Live only; the formatter. |
| 18 | Immigration empty state | Any document | No live immigration document gives "No immigration documents yet", plus the signpost when void ones exist. |
| 19 | Family-permit signpost (`ImmigrationTab.tsx:281`, `:878`) | All family immigration documents | Holding count: live only. |
| 20 | Documents pill (`DocumentsTab.tsx:67-75`) | Removed / Valid / Processing / Rejected | Precedence. `verified` reads "Valid" in `valid`/`expiring`, "Expired" when expired, "Verified" when undated. `received` reads "Processing". |
| 21 | Documents Expires cell (`DocumentsTab.tsx:87-100`, `:334-335`) | ceil countdown; raw `formatDate` | The formatter. Live documents: the ladder, urgent per the formula with window 90. Void documents: the date only. |
| 22 | Documents "expiring soon" totals (`DocumentsTab.tsx:209-224`) | Any status | Holding count: `live && daysLeft !== null && daysLeft <= 90`. Memo dependencies include the card's day. |
| 23 | Documents list, groups, strip count, badge (`ClientDetailClient.tsx:523`, `:1019`) | General documents only | General live documents by category, plus "Removed or rejected". All are listing counts. |
| 24 | Documents empty state | No general documents | "No documents on file yet." only when the tab has no rows at all. |
| 25 | FamilyTab rows (`FamilyTab.tsx:170-260`, `:477-492`) | Enum; ceil; `.find()` | `familyPermit` / `familyPassport`, the ladder and the formatter. |
| 26 | Family void documents | `.find()` may show one | Not shown on members; signpost. |
| 27 | Overview family rows (`OverviewTab.tsx:455-466`) | Member fields only; enums | The same `familyPermit` / `familyPassport` result as row 25. |
| 28 | ProcessTab practice chip (`ProcessTab.tsx:370-390`) | ceil; windows `< 0`, `0..14`, `15..30`; `alert_color` beyond; superseded neutral | A practice, not a document: no liveness, no `dateValidity` state. **Only the count changes** to `dayNumber(expiry) − dayNumber(day)` under the date grammar, with the formatter. Windows, `alert_color` fallback, superseded-first precedence and labels are unchanged. A null date shows no chip. |
| 29 | Tax SPT "Filed" (`sptFilings`) | Excludes archived, removed | Requires `live`. |
| 30 | Coretax / EFIN on file (`coretax.ts:37`) | Excludes archived, removed | Requires `live`. |
| 31 | `companyDocumentToClientDocument` | Forces `is_archived: false`, `deleted_at: null` | Maps `status` as received; Rule L reads it. (Today all are `active`.) |
| 32 | Company vault slots | Drive id present | Filled only by a live document. A void PMA document gets the signpost. |
| 33 | "N documents on file" (`CompanyTab.tsx:584`, `CompanyIdentity.tsx:127`) | Every document | Holding count: live only. |
| 34 | `FilesBrowserPanel` | Drive folder listing | Out of the promise (see Visibility). Unchanged. |
| 35 | AiSummaryCard (server L1 summary) | Server | Unchanged; it does not read documents. |

## Not in scope

- The SQL of `alert_color`, `visa_alert` and `passport_alert`. It is read under the enum
  rule.
- Practice statuses.
- The status union: `crm.types.ts:389` gains `active` and `google_drive`. This is a type
  change only.

## Test obligations for the implementing lane

1. **Backend:**
   - the profile returns `as_of` equal to the `today` passed to `build_client_attention`,
     on both the success and the failure branch;
   - the OpenAPI pin is unchanged.
2. **Rule L truth table, mirrored from `_usable`:**
   - `deleted_at`: null, undefined, `""`, a timestamp;
   - `is_archived`: true, false, null, undefined;
   - `status`: `rejected`, `REJECTED`, `" rejected "` (live), `deleted`, `archived`
     (live), `expired` (live), null, unknown.
3. **Rule D:**
   - grammar: `YYYY-MM-DD`; a date followed by a time (no conversion); `2026-02-30`;
     a basic/week form (null); non-string; empty;
   - `daysLeft` −1/0/1 and 60/61;
   - a `status: "expired"` document with a future date stays `valid`/`expiring`.
4. **One day:**
   - With the browser day past `as_of` and the refetch not yet landed, every surface still
     shows the old `as_of` day: server ledger rows, counters, local rows, the Visa card,
     the Immigration panel, ValidityTrack, Documents cells and totals, Family rows.
   - When the refetch lands with a new `as_of`, they all change in the same render.
   - The midnight timer and the focus check trigger the invalidation.
   - A static test finds no `Date.now()` and no `86400000` in this folder outside
     `WitaClock` and `overview-brief.ts`.
5. **Formatter:** with `TZ=America/Los_Angeles` and `TZ=Asia/Makassar`, a date-only
   expiry renders the same calendar day in the Documents cell, the history text, the
   Family row and the Process chip.
6. **Precedence and urgency:**
   - void with a future date: reason only, not urgent;
   - `verified` in each state;
   - `alert_color: red` at +200 days stays urgent;
   - a live expired document with `alert_color: green` is urgent.
7. **Family:**
   - NULL `visa_expiry` with enum `green` and an expired live fallback document reads
     "Expired" on both FamilyTab and Overview;
   - enum `expired` with a model `valid` reads "Expired";
   - a rejected fallback document is never picked.
8. **Numerals:** an unparseable expiry or issue date shows no numeral, never 0.
9. **Windows:** boundary pairs as follows. An undated live document is never renewable,
   "expiring soon", or "Valid until".
   - 0/1, 60/61, 90/91, 180/181, 269/270 and 419/420 on their surfaces;
   - ProcessTab −1/0, 14/15 and 30/31, plus a superseded expired practice that stays
     neutral.
10. **Pickers:** `pickCurrentVisa.guard.test.ts` passes unchanged. A rejected farthest
    expiry is never picked; its live twin is.
11. **Visibility and counts.** For a void fixture of each kind (passport, own permit,
    family permit, PMA, general, tax):
    - it appears exactly once, in "Removed or rejected";
    - it appears in no domain list or holding count;
    - the right signpost shows;
    - the listing counts equal the rows;
    - `FilesBrowserPanel` is untouched.
12. Every existing test that pins old behaviour changes in the same PR and is named in the
    PR body.
13. **Mutation:** one mutant per rule. The PR body reports killed/total and names any
    survivor. The rules:
    - L1, L2, L3 and no-trim;
    - day 0;
    - the `as_of` day source;
    - the enum source rule;
    - the positive-label rule;
    - the urgency formula;
    - each picker filter;
    - each holding count;
    - the 90-day window;
    - the ValidityTrack clamp;
    - the formatter timezone.
14. Synthetic fixtures only.
