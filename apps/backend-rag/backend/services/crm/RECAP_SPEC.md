# Client recap: numbers spec v3.2 (strict parsing)

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
- v3.2 closes the four v3.1 spec-gate findings:
  - Rule L is now the backend predicate character for character: no trimming;
  - the permit code grammar sees every code-shaped token, compound ones included, so an
    alternative can no longer hide;
  - the absence statement I0 is dropped;
  - C7 weighs the unattributed LKPM rows of every client linked to the company.

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
3. **No absence claims.** The recap never says that something is not recorded. A clause
   with no source, or whose records cannot be classified or ordered, is omitted.
4. **Classify before filtering.** A query never drops a row whose classification decides
   whether a statement can be made.
5. **No identity from free text or OCR.** Names and document numbers never decide that two
   records are the same, so nothing is counted that would need it. No free-text field
   (company or client) reaches the text or a prompt. Labels come only from closed sets or
   database catalogues (`visa_types.name`, `practice_types.name`).

**Liveness (Rule L).** This is the backend attention predicate `_usable`, character for
character (`client_attention.py:214-219`):

```py
doc.get("deleted_at") is None and not doc.get("is_archived")
and str(doc.get("status") or "").lower() not in {"rejected", "deleted"}
```

- There is no trimming: `" rejected "` is live.
- No other status voids a document.
- The implementation calls `_usable` itself; it does not copy it.

Only live documents of the client itself (`family_member_id` NULL) feed I1–I3.

## Grammars

| Input | Grammar (anything else is unparseable) |
|---|---|
| date | One of three forms. A `datetime.date` that is not a `datetime`. An **aware** `datetime` (tzinfo set), reduced to its Asia/Makassar (WITA) date. A `str` exactly `YYYY-MM-DD` that is a real calendar date. A naive `datetime` is unparseable. |
| non-negative integer | A Python `int` that is not a `bool` and is ≥ 0. Or a `str` of ASCII digits `0-9` only, in one of two forms: ungrouped without leading zeros (`"0"`, `"1000"`), or grouped in threes by one separator used throughout, `.` or `,`, with a first group of 1–3 digits without a leading zero (`"1.000"`, `"1,000,000"`). Floats (even `5.0`), Unicode or full-width digits, any sign (ASCII `-`, U+2212, `+`, parentheses), spaces, decimals and currency are unparseable. |
| KBLI code | A `str` of exactly five ASCII digits, with no whitespace. |
| code list | The value at `custom_fields.<key>`. **Empty:** missing, `None` or `[]` means no codes. **Accepted:** a `list` whose every element is a `str`, or a `str` that decodes once as JSON to such a list. **Malformed:** anything else (dict, number, nested list, non-string element, undecodable string). |
| LKPM period | **year:** an `int`, not a `bool`, in 2000–2100. **quarter:** a `str` exactly `Q1`, `Q2`, `Q3` or `Q4` (production stores text), or an `int`, not a `bool`, in 1–4. It is always stated as `Q<n> <year>`. |
| permit family and code of a document | See "Permit documents" below. |
| category / role / status / legal form | An exact match after trimming whitespace and lower-casing, against the closed sets below. Never a substring match. |

## Permit documents

The recap does not take a family or code from `resolve_permit_label`. That resolver keeps
the first family keyword and the first parsable token, so it can hide an alternative. The
recap reads the same inputs under these rules instead.

**Signals.** A document has two signals:

| Signal | Read as |
|---|---|
| `document_type` | A `str`, or absent when NULL. |
| OCR `visa_type` | The path is `ocr_extracted_data` → `raw_response` → `visa_type`. **Absent** when any step is NULL or missing. **Malformed** when `ocr_extracted_data` or `raw_response` is present but not a `dict`, or `visa_type` is present but not a `str`. |

Before matching, each string signal has every `_` replaced by a space.

**Family keywords.** Families come from the keyword tests of `permit_label._classify`.
Each family's test is evaluated on its own; the first match does **not** win. The
families are KITAP, MERP, KITAS, ITK and EVISA. A document's **family set** is every family
whose test matches either signal.

**Code-shaped tokens.** These are the alphanumeric tokens of either signal, upper-cased,
that have at least one full-length parse under the resolver's index grammar
(`_parse_index_token`: `CODE = [A-E]\d{1,2}[A-Z]?`, optionally fused with
`STAY = 2[A-Z]\d{0,2}`).
- A token **reads as a code** when exactly one of its parses has a CODE in
  `visa_types.code`.
- A token with no such parse, or with two or more, is **unresolved**.

A document is a **permit document** when:
- it is the client's own and live;
- and at least one of these holds:
  - its family set is non-empty;
  - it has a code-shaped token;
  - a signal is malformed;
  - its trimmed, lower-cased `document_type` is `visa` or `immigration_document`.

A permit document is **classifiable** when all of these hold:
- no signal is malformed;
- no code-shaped token is unresolved;
- all code-reading tokens read as the same single code;
- its family set has exactly one family.

Otherwise it is **unclassifiable**. That covers two families, two codes, a code outside
the catalogue, an ambiguous compound token, a code with no family keyword, and a generic
type with no keyword.

A classifiable document's **code** is the single index code. When it has none, the code
is its family's generic code: `KITAS`, `KITAP` or `EVISA`.

**Stay-permit families** are KITAS, KITAP and EVISA. ITK is a visit stay permit; it feeds
I3 only. MERP feeds nothing.

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
| I0 | "No current stay permit is recorded" | **Not stated** (Rule 0.3). An ITK, an unclassifiable document or an undated permit would each make it false. |
| I1 | Current permit code and official name | **Candidates:** the client's classifiable permit documents (see Permit documents) whose family is a stay-permit family. **Omitted (with I2, I3 and I6) when:** there is no candidate; any permit document of the client is unclassifiable; or any candidate lacks a strict expiry. In those cases "latest" cannot be decided. **Pick:** the latest expiry. **Tie rule:** tied documents must share one family. Within it, a precise index code beats the same family's generic code. Two different precise codes, or two families, on the latest date omit the clause. **Name:** the official name comes from `visa_types` by the picked code. |
| I2 | Permit issue date, expiry date, "valid" / "expired" | **Expiry:** the I1 date. **Issue date:** the strict OCR issue date of the picked document. When several documents tie with the picked code, it is stated only if all of them carry the same strict issue date. Otherwise the issue date is omitted. **Validity:** "expired" means expiry < today (WITA); otherwise the permit is "valid". |
| I3 | ITK expiry and its status | **Candidates:** the client's classifiable permit documents whose family is ITK. **Omitted when:** any candidate lacks a strict expiry, or I1 is omitted. **Eligible:** with an I2 issue date, the ITKs whose expiry is on or after it. Without one, the ITKs whose expiry is from today up to the I1 expiry. **Statement:** the latest eligible expiry; its status uses the I2 rule. |
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
| C7 | Latest LKPM period (and its status label) | **Candidates:** `lkpm_reports` rows with `company_id` equal to this company. **The company's clients:** every `client_id` in `client_company_links` with this `company_id`. The current client is one of them, by `resolve_company_id`. **Omitted when:** any of the company's clients has a client-level row (`company_id` NULL), because such a row cannot be attributed and could be this company's latest. Whichever linked client opens the recap, the answer is the same. Also omitted when any candidate's period is unparseable. **Statement:** the latest candidate period, as `Q<n> <year>`, with its status label when the status is in the LKPM status set. |

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
2. **Counterexamples from the v3 and v3.1 spec gates.** Each one is a guilt test, written
   with synthetic codes, and each states its expected outcome.
   - An undated KITAS beside a passport: I1 is omitted, and no absence sentence exists.
   - A single live ITK and no stay permit: I1 and I3 are omitted, and no absence sentence
     exists.
   - Rule L on `" rejected "`: the document is live, exactly as `_usable` says.
   - `"E23 or E28A"`: two codes, so unclassifiable.
   - A compound token (CODE fused with STAY) followed by `"or <code>"`: two codes, so
     unclassifiable.
   - A code-shaped token outside the catalogue beside a catalogue code: unresolved, so
     unclassifiable.
   - A `visa_type` that is a `dict`, a list or a number: malformed, so unclassifiable,
     even with `document_type` `kitas`.
   - A `raw_response` that is not a `dict`: malformed.
   - An `ocr_extracted_data` of NULL with `document_type` `kitas`: classifiable as
     `KITAS`.
   - `document_type` `visa` with no OCR: unclassifiable (no family).
   - A `document_type` matching two family keywords: unclassifiable.
   - A generic KITAP tied with a precise E23: two families on the latest date, so I1 is
     omitted.
   - Two E23 with the same expiry and different issue dates: the issue date is omitted.
   - A rejected ITK later than the live one: ignored.
   - The role `not a shareholder`: unknown, so the count is omitted.
   - A `kitas` practice with an empty or misspelled status: I7 is omitted.
   - A client-level LKPM row of the current client: C7 is omitted.
   - A client-level LKPM row of ANOTHER client linked to the same company: C7 is omitted
     whichever linked client opens the recap.
   - A `2027/"q4 "` row beside `2026/Q2`: C7 is omitted.
   - `kbli_codes` cases:
     - a valid list: accepted;
     - a JSON string that decodes once to a valid list: accepted;
     - a dict, a nested list, a list with a non-string element, or an undecodable string:
       malformed, so C5 is omitted.
   - A free-text `company_type`: C1 is omitted.
3. **One omission test per rule.** For every "omitted when" rule, a test where exactly one
   member triggers it, with the clause absent from the rendered text, and its innocent
   twin present.
4. **Query shape.** A test shows the practice query keeps every status and every category,
   and the LKPM query returns the client-level rows of every client linked to the company to the builder.
5. **End to end.** A test runs every statement from DB-shaped rows through the builder,
   the payload and the template.
6. **PII.** A test pushes sentinels through every input and asserts them absent from the
   payload, the prompt, the cache row and the logs.
7. **Mutation per rule.** Every mutant is killed, or the PR body names each survivor and
   why it is equivalent.
