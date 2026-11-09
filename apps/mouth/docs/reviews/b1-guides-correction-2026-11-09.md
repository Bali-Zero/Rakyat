# B1 / e-VOA guide correction — unpublished review candidate

## Scope and release status

Two guide families, each in English, Indonesian, Italian, French and Russian:

- `e-voa-electronic-visa-on-arrival-guide`
- `tourist-visa-extension-indonesia-guide`

Prepared from Rakyat `298a3c790d8806df536935e9d793ac50296261d8` in the isolated
`agent/nuzantara/mouth/b1-guide-correction-1109` branch. The initial mandate was
preparation only. On 2026-11-09 the owner subsequently authorized commit and merge
for inclusion in the next deployment. A separate deployment is not authorized;
required review gates remain in force.
The live pages still carried the incorrect initial 60-day / maximum 120-day
claims when fetched on 2026-11-09. Local corrections are not live corrections.

## Verified source basis (2026-11-09)

- [Ngurah Rai foreign-national services](https://ngurahrai.imigrasi.go.id/Layanan%20WNA/):
  VOA initial 30 days and one extension; e-VOA extension application online,
  followed by an immigration-office photo/interview. Local filing guidance
  must not be generalized to every office. The page gives inconsistent
  formulations for the opening date; no universal opening window is asserted.
- [Jakarta Pusat B1](https://jakartapusat.imigrasi.go.id/layanan/warga-negara-asing-wna/visa-republik-indonesia/b1-visa-saat-kedatangan-wisata):
  B1 is single-entry, initially 30 days, one further 30-day extension.
  Conversion/bridging is conditional, not a guaranteed alternative or categorically impossible.
- [Jakarta Pusat C1](https://jakartapusat.imigrasi.go.id/layanan/warga-negara-asing-wna/visa-republik-indonesia/c1-visa-wisata):
  C1 is a separate single-entry visitor visa, initially up to 60 days,
  with approved extensions up to a total of 180 days. The old four-by-30-day
  arithmetic is not supported by this source.

The central B1/C1 URLs returned HTTP403 to the research tool. The local immigration
office pages above were accessible; no access restriction was bypassed.

## Editorial decisions

Correct the full claim wherever it appears: description, excerpt, SEO,
answer snippets, FAQ, body, comparisons, examples and translations. Preserve
safe application advice, language variants, article identity and cover assets.
Remove unsupported approval guarantees, remote-work loopholes, automatic border-run
outcomes, no-office promises and speculative visa/pricing comparisons.

No new commercial price is selected. Existing catalogue/tests quote issuance at
IDR750,000, while the available VOA product guidance quotes IDR790,000; both quote
IDR850,000 for the extension. The current PricingTool response was not available.
Commercial amounts are omitted pending reconciliation, rather than guessed or
changed in the catalogue. The price regression test permits omission while
retaining catalogue parity when prices are quoted and rejecting known stale bundles.

## Release conditions

- Independently review every corrected language, including substantive sections
  replaced because their former claims were unsupported.
- Obtain the repository-required independent release gate before landing.
- Reconfirm current office procedure and any price before a quote or publication.
- Commit/merge authorization was received on 2026-11-09. Do not bypass the
  independent gate or initiate a separate deployment.
- After authorized publication, verify all ten URLs, SEO/FAQ output and article
  exports; this preparation does not satisfy live proof.

## Adjacent findings outside this mandate

Search also found inconsistent VOA durations in the separate visa-free/e-VOA
comparison, overstay-penalties and overstay-case-studies guides. They are not
silently changed here. Their latest contents require a separate scoped review.

## Verification receipt

- Final content/blog, article-claim ratchet and production MDX compilation checks:
  **29 test files, 1,129 tests passed**. Both guides compile in all five locales.
- New focused B1 factual/provenance and price/MDX checks: **35 passed** (included
  in the count above). Before edits, the new factual guard failed all ten old
  guide files while its eleven original guilt/innocence cases passed.
- Existing article-export integration checks: **8 passed** after allowing the
  local temporary IPC pipe used by tsx; no publication performed.
- Prettier check and `git diff --check`: passed.
- Typecheck: blocked by three `DataCollection.mcp` errors in unchanged Sentry
  config files. Reproduced the same errors in the unchanged source checkout;
  this candidate does not modify those configs or shared dependencies.
- Explicit ESLint of tests: blocked by the installed eslint-plugin-react/ESLint
  API incompatibility (`contextOrFilename.getFilename is not a function`).
  The default lint configuration ignores these tests, so its warnings are not
  reported as a successful lint verdict.
- Full production build and browser/live post-publication QA: not run. Nothing
  was deployed; production still needs the separately authorized release path.
- Independent read-only advisory review covered the core claims, translations
  and new lexical ratchet matches. All new guarantee-word hits are explicit
  negations/cautions. Only the two guide families' baseline entries changed;
  unrelated allowances and the ratchet implementation are unchanged. This
  advisory review is not the repository-required cross-family release gate.

The correction is intentionally larger than a numeral replacement: unsafe
pricing, fictional timing, work advice and guarantee sections were rewritten
or removed. All ten language versions and useful section topics remain.
