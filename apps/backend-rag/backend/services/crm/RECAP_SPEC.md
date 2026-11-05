# Client recap: numbers spec v3 (strict parsing)

Status: **proposed, gated before code**. Owner decision 2026-11-05: the recap stays
off (`client_recap.RECAP_ENABLED = False`, PR #31) until this spec passes its gate
and a lane implements it. That lane flips the switch in the same PR.

Lineage: v1 and v2 lived in the `client_recap` module docstring on #22 (gate rounds
2–7). Round 7 still found counts that were not exact. Both came from inputs repaired
or filtered before they were judged:

- an empty practice category was dropped in SQL before the "unknown category" rule;
- a Unicode minus was stripped, so "−5" read as 5 shares.

v3 replaces case-by-case rules with one class rule.

## Rule 0: the class rule

The recap states only two kinds of value.

- **(a) A recorded value**, read from a typed field and shown as recorded: a date, an
  amount, a code, a period or a label.
- **(b) A count over identities that the database defines**: row primary keys, client
  ids, or codes that pass their grammar.

Every input is parsed **strictly** against its grammar below.

- A value that does not match exactly is **unparseable**.
- An unparseable value is never repaired: no stripping of characters, no sign guessing,
  no rounding, no case folding beyond what the grammar says.
- An unparseable or unknown input never contributes to a stated value:
  - an unparseable recorded value is omitted;
  - a count with **any** unparseable or unknown member is omitted as a whole, never
    stated as a lower or partial number.
- Filtering happens only after classification. A query never drops a row whose
  classification decides whether a count can be stated.
- **No identity is inferred from free text or OCR.** Names and document numbers are
  never used to decide that two records are the same thing, so nothing is counted that
  would need such an inference.

## Grammars

| Input | Grammar (anything else is unparseable) |
|---|---|
| date | A `date`/`datetime` object, or the string `YYYY-MM-DD`. A timestamp is reduced to its date in Asia/Makassar (WITA). |
| non-negative integer | A Python `int` ≥ 0 that is not a `bool`, or a string of ASCII digits, either ungrouped (`"1000"`) or grouped in threes by one separator used throughout (`"1.000"`, `"1,000,000"`). There is no sign of any kind (ASCII `-`, U+2212, parentheses), no decimals, no spaces and no currency. |
| KBLI code | Exactly five ASCII digits, after trimming surrounding whitespace. |
| LKPM period | `year` is an integer 2000–2100; `quarter` is the string `Q1`–`Q4` (the production column is text), or the integer 1–4. |
| category / role / status | Compared after trimming and lower-casing only, against the closed sets below. |

## Closed sets

- **Immigration practice categories:** `kitas`, `kitap`, `visa`, `visa_extension`,
  `single_entry_visa`, `multiple_entry_visa`.
  - These are measured on production `practice_types` (2026-11-05). The live recap and
    #22 knew only `visa`, `visa_extension` and `kitap`, so open KITAS practices were not
    counted. That was a live inexactness that no round caught.
- **Non-immigration practice categories:** `company`, `corporate`, `property`, `tax`,
  `tax_annual`, `tax_monthly`, `tax_monthly_bundled`, `tax_standalone`.
- **Every other category is unknown.** This includes `other`, `urgent`, `inquiry`, NULL,
  the empty string and any category not listed here.
  - `other` mixes immigration services (EPO, ERP, passport mutation, birth report) with
    non-immigration ones (driving licence, SKCK). It cannot be classified by category.
- **Closed practice statuses:** the existing `_CLOSED_PRACTICE_STATUSES`.
- **Shareholder role:** the role contains `shareholder` or `pemegang saham`.
- **Director role:** the role matches `director` or `direktur`.

## Statements

Each row is one statement the deterministic template can make. Nothing else is stated.

### Immigration tab

| # | Statement | Rule |
|---|---|---|
| I1 | Current permit code and official name | **Candidates:** the client's own documents (`family_member_id` NULL; not rejected, deleted or archived) that the permit resolver puts in a stay-permit family, with a strict expiry date. A document without a strict expiry is not a candidate. **Pick:** the latest expiry wins. On a tie, a precise index code (e.g. `E23`) beats a generic family code (`KITAS`). Two different precise codes on the same date omit the clause. **Name:** the official name comes from `visa_types` by code. |
| I2 | Permit issue date and expiry date, and "valid" / "expired" | As recorded on the I1 document. The issue date comes from its OCR issue-date keys and must be strict, else it is omitted. "Expired" means expiry < today (WITA); otherwise the permit is "valid". |
| I3 | ITK expiry and its status | ITK documents of the client with a strict expiry are considered. When the I1 permit has an issue date, the eligible ITKs are those whose expiry is on or after that issue date. Without an issue date, the eligible ITKs are those whose expiry is from today up to the permit's expiry. The latest eligible expiry is stated as recorded; its status uses the I2 rule. |
| I4 | Passport expiry and its status | `clients.passport_expiry`, a strict date. Its status uses the I2 rule. |
| I5 | Family members on file | The number of `client_family_members` rows of the client (row primary keys). |
| I6 | Family permits that end before the main permit | **Who has a permit:** a row whose `current_visa_type` is non-empty or whose `visa_expiry` is set. **Count:** rows with a permit whose strict `visa_expiry` is earlier than the I1 expiry. **Omitted:** when I1 has no expiry, or any row with a permit lacks a strict `visa_expiry`. |
| I7 | Open immigration practices: count, and up to four type names | **Query:** every open practice row of the client (status not closed) with its category, with the type resolved by id, or by code for a legacy row, and with **no category filter in SQL**. **Count:** rows whose category is an immigration category. **Omitted:** when any open row has an unknown category. **Names:** practice type names are display labels and never counted. |
| — | Earlier permits | **Not stated.** A count would need identity from OCR document numbers (Rule 0). This replaces v2's permit-number identity. |

### Company tab

| # | Statement | Rule |
|---|---|---|
| C1 | Legal form | `companies.company_type`, as recorded. |
| C2 | Establishment date; last amendment date | `akta_pendirian_date` and `akta_perubahan_date` respectively, each a strict date. The generic `custom_fields.akta_date` is never an amendment. |
| C3 | Authorised and paid-up capital | `custom_fields.authorized_capital` / `paid_up_capital`, each a strict non-negative integer (IDR), else omitted. Written in words only when the one-decimal short form equals the amount exactly; otherwise in full digits (`IDR 1,049,000,000`). |
| C4 | Shareholders and directors "linked in the CRM" | Counted only from `client_company_links` of the company, as distinct `client_id`. **Shareholder:** a link is one when its role is a shareholder role, or when `shares_count` (an integer column) is greater than 0. **Director:** a director role. **Not counted:** the company profile register (`custom_fields.shareholders`), because its identities are free-text names (Rule 0). This replaces v2's register count. The sentence always says "linked in the CRM". |
| C5 | KBLI lines: count and up to two titles | The distinct codes among `companies.kbli_code` and `custom_fields.kbli_codes`. **Omitted:** the whole clause, when any present code is not a strict KBLI code. |
| C6 | Codes not in the KBLI 2025 catalogue | The distinct C5 codes for which the catalogue answers ABSENT. **Stated only when:** C5 is stated, every code is classified (no UNKNOWN verdict), and there are at most 50 codes. The wording is exactly "not in the KBLI 2025 catalogue", never "KBLI 2020". |
| C7 | Latest LKPM period | The company's latest strict period: rows of this company, or client-level rows (`company_id` NULL) when the client has exactly one linked company. Year and quarter are stated as recorded. Rows whose period is unparseable are not candidates. |

**Never stated:** total shares, any name (client, family, shareholder, staff, company),
permit, passport, NIB, NPWP or AHU numbers, addresses, contacts, sponsors and file names.

## Output boundary and logging

- Only the `recap_payload` allow-list reaches the template, the cache or a prompt.
- No log line carries a client field value. That includes KBLI codes, permit codes and
  numbers, and counts tied to a named record. Exception **type names** and reason codes
  only.
- This closes the round-7 finding on `kbli_catalogue_membership`: its warning drops the
  code.

## Cache and switch

- `RECAP_SCHEMA_VERSION` is bumped, so every cached text written under older rules
  regenerates on its next view.
- The implementing PR sets `RECAP_ENABLED = True` only after its gate passes on the head
  that implements this spec.

## Test obligations for the implementing lane

1. Every grammar has guilt tests for each class of unparseable input. Examples:
   - U+2212 and parentheses on integers;
   - mixed separators;
   - decimals and spaces;
   - four- or six-digit KBLI codes;
   - `Q5` and `q1 ` periods;
   - malformed dates.
2. Every "omitted" rule has a test where exactly one member is unknown and the clause is
   absent from the rendered text.
3. A query-shape test shows the practice query keeps rows of every category and every
   open status.
4. A test runs every statement end to end (DB-shaped rows → builder → payload →
   template).
5. A PII test pushes sentinels through every input and asserts them absent from the
   payload, the prompt and the logs.
6. Mutation on each rule: the lane reports the killed/total count.
