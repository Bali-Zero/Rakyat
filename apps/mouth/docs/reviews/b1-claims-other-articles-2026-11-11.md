# B1 / e-VOA claim correction in two further article families — unpublished review candidate

## What was wrong

The site stated that the B1 visa on arrival / e-VOA gives 60 days, extendable twice to 120 days.
PR #128 corrected the two guide families. The same claim still stood in:

- `kitas-cancellation-company-closure` (EN, ID, IT, FR, RU): one passage in the re-entry options
  ("60 days stay (extendable to 120 days)") and its four translations.
- `visa-free-vs-evoa-indonesia-comparison` (EN, ID, IT, FR, RU): the whole article was built on it
  (SEO fields, answer snippet, FAQ, comparison tables, "second extension days 91-120", a "120 days for
  IDR 1.5 million" total, decision matrix, scenarios, conclusion).

## Rule-pack lines relied on

`apps/backend-rag/backend/services/visa_engine/contracts/packs/rulepack-prod-033.source.json`, B1 record:

- 7661-7664 `stay_policy`: FIXED_DAYS, minimum and maximum 30 days.
- 7671-7672 `entry_count`: SINGLE.
- 7686-7689 `extension_policy`: allowed, `days_per_extension` 30, `maximum_extensions` 1.
  Rule: 30 days on entry, one extension of up to 30 days, 60 days at most.
- 7692 onwards `prohibited_activities`: paid employment, business meetings, study, journalism.
- C1 record (7700-7739): 60 days, two 60-day extensions (180 total), used only to say C1 is a different visa.

## What changed

- KITAS article: only the one wrong line, in all five languages. `source_sha256` of the four
  translations recomputed.
- Comparison article: rewritten around the right numbers in EN, ID, IT; FR and RU corrected in
  the sections they carry. Everything that rested on 60/120 days, on per-extension fees, on visa-run
  costs or on remote-work "gray area" advice was rewritten or deleted (remote-work and
  repeated-entry advice follows the editorial line of PR #128). Table 2 now maps trip length to the
  route that covers it, with no money. The metadata, FAQ (one question added: how long can I stay)
  and `updatedAt` (2026-11-11) are corrected in all five languages. FR and RU had a fully English
  frontmatter: description, excerpt, seoDescription, answerSnippet and the FAQ are now translated.
  `source_sha256` was added to FR and RU, as ID and IT already had it.
- Money: the only amounts stated are Bali Zero's catalogue prices (`single_entry_visas`:
  B1 VOA 750.000, +30-day extension 850.000, government fee included) and the visa-free price of 0.
  The government fee is not in the rule pack, so readers are sent to the official portal.
  Removed: IDR 500,000, USD 32, per-extension fees, IDR 1.5 million total, visa-run flight costs,
  overstay fine amounts, agent fees, KITAS/B211A price ranges.
- Guard: `b1-guides.truth.test.ts` now runs the four wrong-claim patterns on both families
  (ten files), pins the comparison article's metadata, FAQ and amounts (only 750000 / 850000
  allowed), and checks translation hashes and heading alignment. No existing assertion changed.
- Claims ratchet: unchanged, passes; no baseline entry added or removed.

## Not reviewed (outside the B1 rule)

- Visa-free and e-VOA nationality lists, the "~20" and "90+" country counts, "~150 countries",
  Table 4 and the "countries requiring a visa" block (still mention B211A "60 days, extendable to 180").
  Doubtful: Vietnam is listed twice; "Indonesia is the 10th ASEAN member" reads as a drafting slip;
  Israel "recently added" is undated; the ASEAN list is stated to be 10 countries and lists 9.
- Visa-free permitted-activities list (social, family visits) and the "no conversion" claims.
- KITAS article procedure, timelines, "Available immediately after exit" and "IDR 15M - 25M" for a
  new KITAS. Those were left untouched.
- Doubtful, rule pack vs catalogue: the B1 record lists `business_meetings` as prohibited, while the
  catalogue description of the B1 VOA says "short business meetings". The corrected articles follow
  the rule pack (tourism only); the catalogue wording needs an owner decision.
- FR and RU comparison translations still end after the cost-by-length table (five sections, as
  before). Not translated and left alone: `title`, `seoTitle`, `coverImageAlt` and
  `aiOptimization.primaryQuestion` in FR and RU (English, none of them carries the wrong rule),
  and the sections after Table 2 that those two files never had.
- Live pages were not fetched and nothing was deployed.
