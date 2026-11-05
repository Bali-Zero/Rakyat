# Client recap: numbers spec v3.1 (strict parsing)

Status: **proposed, gated before code**. Owner decision 2026-11-05: the recap stays
off (`client_recap.RECAP_ENABLED = False`, PR #31) until this spec passes its gate and a
lane implements it. That lane flips the switch in the same PR.

Lineage:
- v1 and v2 lived in the `client_recap` module docstring on #22, through gate rounds 2–7.
- Round 7 still found counts that were not exact. Both came from inputs that were
  repaired or filtered before they were judged:
  - an empty practice category was dropped in SQL;
  - a Unicode minus was stripped.
- v3 replaced case rules with one class rule.
- v3.1 closes the ten gaps the v3 spec gate found:
  - absence claims;
  - ambiguous permit codes;
  - ties;
  - ITK exclusions;
  - role grammar;
  - unknown practice statuses;
  - LKPM attribution;
  - naive datetimes;
  - list containers;
  - free-text labels.

## Rule 0: the class rule

The recap states only two kinds of value.

- **(a) A recorded value**, read from a typed field and shown as recorded: a date, an
  amount, a code, a period, or a label from a closed set or a database catalogue.
- **(b) A count over identities the database defines**: row primary keys or client ids.

Five rules apply to every input and every statement.

1. **Strict parsing.** Every input is parsed against its grammar below. A value that does
   not match exactly is **unparseable**. It is never repaired: no stripping of characters,
   no sign guessing, no rounding, no case folding or trimming beyond what the grammar says.
2. **Omit, never understate.**
   - An unparseable recorded value is omitted.
   - A count, a selection ("the current permit", "the latest") or an ordering whose input
     set contains **any** member that is unparseable, unknown, or would need a judgement
     the grammar does not make is omitted as a whole. It is never stated as a lower,
     partial or "best guess" value.
3. **Absence is a statement too.**
   - "No X is recorded" is stated only when the source set is **empty**: no live record of
     that kind exists at all.
   - When records exist but cannot be classified or ordered, the clause is omitted. It is
     never replaced by an absence claim.
4. **Classify before filtering.** A query never drops a row whose classification decides
   whether a statement can be made.
5. **No identity from free text or OCR.** Names and document numbers never decide that two
   records are the same, so nothing is counted that would need it. No free-text field
   (company or client) reaches the text or a prompt. Labels come only from closed sets or
   database catalogues (`visa_types.name`, `practice_types.name`).

**Liveness (Rule L, the backend attention predicate,
`client_attention.py:216-218`).** A document is live when all three hold:
- `deleted_at` is NULL;
- `is_archived` is not true;
- `status`, trimmed and lower-cased, is neither `rejected` nor `deleted`.

Only live documents of the client itself (`family_member_id` NULL) feed I0–I3.

## Grammars

| Input | Grammar (anything else is unparseable) |
|---|---|
| date | One of three forms. A `datetime.date` that is not a `datetime`. An **aware** `datetime` (tzinfo set), reduced to its Asia/Makassar (WITA) date. A `str` exactly `YYYY-MM-DD` that is a real calendar date. A naive `datetime` is unparseable. |
| non-negative integer | A Python `int` that is not a `bool` and is ≥ 0. Or a `str` of ASCII digits `0-9` only, in one of two forms: ungrouped without leading zeros (`"0"`, `"1000"`), or grouped in threes by one separator used throughout, `.` or `,`, with a first group of 1–3 digits without a leading zero (`"1.000"`, `"1,000,000"`). Floats (even `5.0`), Unicode or full-width digits, any sign (ASCII `-`, U+2212, `+`, parentheses), spaces, decimals and currency are unparseable. |
| KBLI code | A `str` of exactly five ASCII digits, with no whitespace. |
| code list | The value at `custom_fields.<key>`. **Empty:** missing, `None` or `[]` means no codes. **Accepted:** a `list` whose every element is a `str`, or a `str` that decodes once as JSON to such a list. **Malformed:** anything else (dict, number, nested list, non-string element, undecodable string). |
| LKPM period | **year:** an `int`, not a `bool`, in 2000–2100. **quarter:** a `str` exactly `Q1`, `Q2`, `Q3` or `Q4` (production stores text), or an `int`, not a `bool`, in 1–4. It is always stated as `Q<n> <year>`. |
| permit code of a document | **String signals only:** `document_type` and the OCR `visa_type`, each used only if it is a `str`. A non-string OCR value counts as absent. **Index code:** a token equal to a `visa_types.code`. **Classifiable:** every index code found across the document's string signals is the same single code, and the permit resolver's family for the document is unambiguous. The document's code is then that index code, or, when no index code is found, the family's generic code (`KITAS`, `KITAP`, `EVISA`). **Unclassifiable:** two distinct index codes, two families, or an index code whose family differs from the resolved family. |
| category / role / status / legal form | An exact match after trimming whitespace and lower-casing, against the closed sets below. Never a substring match. |

## Closed sets

All sets are measured on production on 2026-11-05.

**Practice categories:**
- **Immigration:** `kitas`, `kitap`, `visa`, `visa_extension`, `single_entry_visa`,
  `multiple_entry_visa`.
- **Non-immigration:** `company`, `corporate`, `property`, `tax`, `tax_annual`,
  `tax_monthly`, `tax_monthly_bundled`, `tax_standalone`.
- **Unknown:** anything else, including `other`, `urgent`, `inquiry`, NULL and the empty
  string. `other` mixes immigration services (EPO, ERP, passport mutation) with
  non-immigration ones (driving licence, SKCK).

**Practice statuses:**
- **Open:** `on_process`, `sending_invoice`, `waiting_documents`, `inquiry`.
- **Closed:** `completed`, `cancelled`, `canceled`, `closed`, `done`, `rejected`.
- **Unknown:** anything else, including NULL and the empty string.

**CRM link roles** (`client_company_links.role`):
- **Shareholder:** `shareholder`, `pemegang saham`.
- **Director:** `director`, `direktur`, `direktur utama`, `director_utama`,
  `president_director`, `presiden direktur`.
- **Other known (neither):** `commissioner`, `komisaris`, `komisaris utama`,
  `presiden komisaris`, `contact`.
- **Unknown:** anything else, including `owner`, `badan hukum`, NULL and the empty string.

**LKPM statuses (label only):** `draft`, `pending`, `submitted`, `approved`. Any other
status omits the label, not the period.

**Legal forms (C1):** `pt pma`, `pt`, `pma`, `cv`, `yayasan`, `ud`,
`pmdn non fasilitas`, `swasta nasional`. The value is stated as recorded (trimmed). Any
other value, including the generic `Company`, is omitted.

## Statements

Each row is one statement the deterministic template can make. Nothing else is stated.

### Immigration tab

| # | Statement | Rule |
|---|---|---|
| I0 | "No current stay permit is recorded" | Stated only when the client has **no** live document that the permit resolver places in a stay-permit family (KITAS, KITAP, e-Visa), whatever its expiry or code. |
| I1 | Current permit code and official name | **Candidates:** the client's live documents in a stay-permit family. **Omitted (with I2, I3, I6) when:** any candidate is unclassifiable or lacks a strict expiry, because "latest" cannot be decided. **Pick:** the latest expiry. **Tie rule:** tied documents must share one family. Within it, a precise index code beats the same family's generic code. Two different precise codes, or two families, on the latest date omit the clause. **Name:** the official name comes from `visa_types` by the picked code. |
| I2 | Permit issue date, expiry date, "valid" / "expired" | **Expiry:** the I1 date. **Issue date:** the strict OCR issue date of the picked document. When several documents tie with the picked code, it is stated only if all of them carry the same strict issue date. Otherwise the issue date is omitted. **Validity:** "expired" means expiry < today (WITA); otherwise the permit is "valid". |
| I3 | ITK expiry and its status | **Candidates:** the client's live documents in the ITK family. **Omitted when:** any candidate lacks a strict expiry, or I1 is omitted. **Eligible:** with an I2 issue date, the ITKs whose expiry is on or after it. Without one, the ITKs whose expiry is from today up to the I1 expiry. **Statement:** the latest eligible expiry; its status uses the I2 rule. |
| I4 | Passport expiry and its status | `clients.passport_expiry`, a strict date; status by the I2 rule. |
| I5 | Family members on file | The number of `client_family_members` rows of the client. |
| I6 | Family permits that end before the main permit | **Who has a permit:** a row whose trimmed `current_visa_type` is non-empty, or whose `visa_expiry` is not NULL. **Count:** those rows whose strict `visa_expiry` is earlier than the I1 expiry. **Omitted when:** I1 is omitted, or any row with a permit lacks a strict `visa_expiry`. |
| I7 | Open immigration practices: count, and up to four type names | **Query:** **every** practice row of the client, with its status and its category (type resolved by id, or by code for a legacy row), and no status or category filter in SQL. **Count:** rows with an open status and an immigration category. **Omitted when** any row could be an open immigration practice without being counted. That means one of three combinations: (unknown status, immigration category); (unknown status, unknown category); (open status, unknown category). **Names:** the listed names are `practice_types.name` of the counted rows. |
| — | Earlier permits | **Not stated.** It would need identity from OCR document numbers (Rule 0.5). |

### Company tab

| # | Statement | Rule |
|---|---|---|
| C1 | Legal form | `companies.company_type` in the legal-form set, else omitted. |
| C2 | Establishment date; last amendment date | `akta_pendirian_date` and `akta_perubahan_date` respectively, each a strict date. The generic `custom_fields.akta_date` is never an amendment. |
| C3 | Authorised and paid-up capital | `custom_fields.authorized_capital` and `paid_up_capital`. Each is a strict non-negative integer (IDR), else omitted. It is written in words only when the one-decimal short form equals the amount exactly; otherwise in full digits (`IDR 1,049,000,000`). |
| C4 | Shareholders and directors "linked in the CRM" | **Source:** only `client_company_links` of the company, counted as distinct `client_id`. `shares_count` is an integer column. **Shareholder:** a link with a shareholder role, or with `shares_count > 0`. **Not a shareholder:** a link with a director or other-known role and `shares_count` NULL or 0. **Shareholder count omitted when:** any link has an unknown role and `shares_count` NULL or 0. **Director:** a link with a director role. **Director count omitted when:** any link has an unknown role. **Not counted:** the company profile register (`custom_fields.shareholders`), whose identities are free-text names (Rule 0.5). The sentence always says "linked in the CRM". |
| C5 | KBLI lines (count only) | **Codes:** the distinct codes among `companies.kbli_code` (empty when NULL or `""`) and the `custom_fields.kbli_codes` code list. **Omitted when:** the list is malformed, or any present code is not a strict KBLI code. **No titles are stated:** `kbli_description` is company free text (Rule 0.5). |
| C6 | Codes not in the KBLI 2025 catalogue | The distinct C5 codes for which the catalogue answers ABSENT. **Stated only when:** C5 is stated, every code is classified (no UNKNOWN verdict), and there are at most 50 codes. **Wording:** exactly "not in the KBLI 2025 catalogue". |
| C7 | Latest LKPM period (and its status label) | **Candidates:** `lkpm_reports` rows with `company_id` equal to this company. **Omitted when:** the client has any client-level row (`company_id` NULL), since that row cannot be attributed to a company and could be this company's latest; or any candidate's period is unparseable. **Statement:** the latest candidate period, as `Q<n> <year>`, with its status label when the status is in the LKPM status set. |

**Never stated:** total shares, KBLI titles, any name (client, family, shareholder,
staff, company), permit, passport, NIB, NPWP or AHU numbers, addresses, contacts,
sponsors and file names.

## Output boundary and logging

- Only the `recap_payload` allow-list reaches the template, the cache or a prompt. The
  allow-list carries only the values the statements above produce.
- No log line carries a client field value. That includes KBLI codes, permit codes,
  numbers, roles and labels. Exception **type names** and fixed reason codes only.
- This closes the round-7 finding on `kbli_catalogue_membership`: its warning drops the
  code.

## Cache and switch

- `RECAP_SCHEMA_VERSION` is bumped, so every cached text written under older rules
  regenerates on its next view.
- The implementing PR sets `RECAP_ENABLED = True` only after its gate passes on the head
  that implements this spec.

## Test obligations for the implementing lane

1. **Type matrix per grammar.** Each grammar is tested against:
   - `bool`;
   - `float`;
   - `dict` and `list`;
   - `None`;
   - Unicode and full-width digits;
   - U+2212, `+` and parentheses;
   - leading zeros;
   - mixed separators;
   - inner and outer whitespace;
   - naive and aware datetimes across the WITA midnight;
   - impossible dates;
   - `Q5`, `q1` and `"Q1 "`.
2. **Counterexamples from the v3 spec gate**, each a guilt test with its expected
   omission:
   - an undated KITAS beside a passport (I0 must not fire);
   - `"E23 or E28A"` and a dict `visa_type` (unclassifiable);
   - a generic KITAP tied with a precise E23;
   - two E23 with the same expiry and different issue dates (issue date omitted);
   - a rejected ITK later than the live one (ignored);
   - the role `not a shareholder` (unknown, so the count is omitted);
   - a `kitas` practice with an empty or misspelled status;
   - a client-level LKPM row (C7 omitted);
   - a `2027/"q4 "` row beside `2026/Q2` (C7 omitted);
   - a dict, JSON-string or nested `kbli_codes`;
   - a free-text `company_type` (C1 omitted).
3. **One omission test per rule.** For every "omitted when" rule, a test where exactly one
   member triggers it, with the clause absent from the rendered text, and its innocent
   twin present.
4. **Query shape.** A test shows the practice query keeps every status and every category,
   and the LKPM query keeps client-level rows visible to the builder.
5. **End to end.** A test runs every statement from DB-shaped rows through the builder,
   the payload and the template.
6. **PII.** A test pushes sentinels through every input and asserts them absent from the
   payload, the prompt, the cache row and the logs.
7. **Mutation per rule.** Every mutant is killed, or the PR body names each survivor and
   why it is equivalent.
