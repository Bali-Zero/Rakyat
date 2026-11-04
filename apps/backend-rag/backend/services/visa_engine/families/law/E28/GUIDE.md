# Guide — Family E28 (investor)

Law: the eight adjacent code cards, captured from official Imigrasi pages on 2026-10-17 and copied verbatim. E28 and E28E say “Data Belum Tersedia”: they remain secondary `NOT_FOUND` cards, `TO_VERIFY` in the DB, with NULL permit class and no Guide path. E28G has substantive official text; its current classification is disputed, not silently declared retired.

```text
Q1  Which investment activity or corporate role will you undertake?
    Shareholder in the sponsoring Indonesian company ................ E28A
    Individual establishing an Indonesian company ................... E28B
    Individual buying government bonds / listed shares / mutual funds  E28C
    Director / commissioner of a foreign company's new Indonesian
      branch or subsidiary ......................................... E28D
      branch or subsidiary in Ibu Kota Nusantara .................... E28F
    Parent-company representative placed in an Indonesian branch .... E28G

Q2  E28A only: do you personally own shares worth at least IDR 10 billion
    (or equivalent) in the sponsoring company registered with
    the Ministry of Investment/BKPM?
    no → NOT_COVERED (the Law directs lower-share directors/commissioners
                      to the working visa for their position)

Q3  E28A only: will that company sponsor your visa application?
    no → NEEDS_SPONSOR
    E28B/C/D/F/G explicitly need no sponsor: skip this question.

Q4  How many months do you need? (positive whole number; asked BEFORE the commitment)
    E28A: ≤12 → 1 YEAR; ≤24 → 2 YEAR; 25–72 → OK_WITH_EXTENSION;
          >72 → STAY_TOO_LONG
    E28B/C/D/F: ≤60 → 5 YEAR; ≤120 → 10 YEAR; >120 → STAY_TOO_LONG
    E28G: ≤60 → 5 YEAR; ≤120 → 10 YEAR; >120 → OK_WITH_EXTENSION,
          total_max_known: false (no cumulative maximum published)
    Fitting initial option → OK + stay_option {value, unit: YEAR}

Q5  E28B/C/D/F/G only: ONE yes/no commitment question for the stay tier actually
    selected by Q4 (smallest fitting option, 5 or 10 YEAR), quoting that tier's
    exact card amount (investment_commitment_<code>_5y / _10y):
      E28B  5y USD 2.5M (placed shares or investment)   10y USD 5M
      E28C  5y USD 350,000 (bonds/shares/funds)         10y USD 700,000, or an apartment USD 1,000,000
      E28D  5y USD 25M                                  10y USD 50M
      E28F  5y USD 5M                                   10y USD 10M
      E28G  5y USD 25M                                  10y USD 50M
    no → NOT_COVERED + stay_option (the tier named). No silent downgrade: an
    applicant who needs 61–120 months and meets only the 5-year amount is
    NOT_COVERED; they must ask for ≤60 months to be judged on the 5-year amount.
    Over the longest stay (>120) → STAY_TOO_LONG / E28G OK_WITH_EXTENSION is
    decided first; E28G extension still asks the 10-year question.
    Extension → largest initial stay_option; E28A total_max_known: true
```

At most four questions on a path; E28B/C/D/F/G ask three (route, months, tier commitment). Missing or invalid answers ask the same question again. `OK` is a navigation/stay result; the complete Law checklist still applies. The routes are not legally exclusive: E28A also allows company establishment and corporate roles. A yes to the tier's investment-commitment question records the applicant's statement only; it does not verify the commitment.

| Question         | Law condition                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Activity / role  | Each `definition_verbatim`: E28B “investor perorangan yang berniat mendirikan perusahaan”; E28C “pembelian obligasi pemerintah, saham pada perusahaan terbuka, atau reksa dana”; E28D/F “anggota direksi atau anggota dewan komisaris” in a foreign branch/subsidiary, with E28F specifying “Ibu Kota Nusantara”; E28G “perwakilan perusahaan induk yang ditempatkan di perusahaan cabang”. E28A permits investing and corporate roles in the company where the applicant invests. |
| Own shareholding | E28A `requirements_verbatim` item 7: “Bukti kepemilikan saham paling sedikit Rp10.000.000.000,00 (sepuluh miliar rupiah) atau setara pada perusahaan penjamin yang terdaftar di Kementerian Investasi/Badan Koordinasi Penanaman Modal.” This is the applicant's holding, not the company's total capital.                                                                                                                                                                         |
| Sponsor          | E28A: “Anda membutuhkan penjamin/sponsor untuk mengajukan visa ini.” The other five substantive pages: “Anda tidak membutuhkan penjamin/sponsor untuk mengajukan visa ini”.                                                                                                                                                                                                                                                                                                        |
| Duration         | E28A `stay_days`: “1 tahun atau 2 tahun”; E28B/C/D/F/G: “5 tahun dan 10 tahun”. `supplementary_stay_evidence.extension_verbatim` gives E28A “tidak lebih dari 6 tahun” and E28B/C/D/F “tidak lebih dari 10 tahun”. E28G has no supplementary table row.                                                                                                                                                                                                                            |

Initial `stay_options` retain the original YEAR unit. Comparisons use exactly 12 calendar MONTHS per YEAR. The DB's existing `max_total_months` stores the exact calendar equivalents of the supplementary table's cumulative limits (6 years = 72 months; 10 years = 120 months). `initial_stay_days` and `max_total_days` remain NULL. No conversion to days, new schema or invented extension count is needed. Extensions do not exceed the first ITAS duration, as the supplementary text states; a navigation verdict does not promise a single extension of the full remaining period.

Documents are a result checklist, never extra questions: passport, living-cost evidence, photo, CV, itinerary, company approval, account statements and all special requirements stay in the cards. Preserve the five-year and ten-year sections separately:

| Code        | Special investment condition in the verbatim checklist                                                                                                                                                                                                                                                                                             |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| E28A        | Applicant's shares at least IDR 10 billion or equivalent; company incorporation approval; two-month company current account. A company younger than two months may submit the account requirement within 90 days after ITAS issuance.                                                                                                              |
| E28B        | Foreign-company shareholding and audited turnover evidence; commitment to establish an Indonesian company with placed shares or investment of at least USD 2.5 million for five years or USD 5 million for ten years, fulfilled within 90 days after entry. The accountant directory is a source reference, not additional applicant requirements. |
| E28C        | Five years: securities at least USD 350,000. Ten years: securities at least USD 700,000 **or** apartment ownership at least USD 1 million. The property alternative is retained; it is absent from the securities-only definition/summary, so no independent property route or five-year property option is inferred.                              |
| E28D / E28G | Foreign-company investment commitment of at least USD 25 million for five years or USD 50 million for ten years, fulfilled within 90 days after entry.                                                                                                                                                                                             |
| E28F        | Foreign-company investment commitment of at least USD 5 million for five years or USD 10 million for ten years, fulfilled within 90 days after entry.                                                                                                                                                                                              |

Prices: only E28A's three exact `kitas_permits` keys in `family_e28.json` (two-year Offshore, Altus/Onshore and Extend). No one-year E28A or Golden Visa price exists in the supplied list; a missing key means unpriced, not confirmed unsold (`is_sold: null` for the other codes). A one-year Law option must not inherit a two-year commercial price. PNBP amounts are reference material, never client prices. `Investor KITAP + MERP` exists under `kitap_permits`, but no code mapping is verified, so it is not attached to an ITAS row.

ITAS → ITAP is a stage of this family. [ITAP.json](ITAP.json) preserves the official text and all eight code assessments verbatim. The central page names “penanaman modal asing” but no E28 code. E28A's generic conversion and the other pages' conversion to another ITAS do not establish ITAP eligibility. `itap` remains NULL for every code; no ITAP question or holding-period threshold is invented.

TO_VERIFY: missing E28/E28E substantive pages; all codes' offshore/onshore application location and per-code ITAP entitlement; E28A's initial-duration conflict (code page 1/2 years, supplementary table 2 years); E28G's current classification and cumulative maximum; E28C's summary/property scope difference; E28B/C/D/F/G's national-passport requirement versus the non-national-document boilerplate. The individual visa pages omit cumulative maxima; the supplementary table supplies them only for E28A/B/C/D/F. The central ITAP page's completion section says “Izin Tinggal Terbatas virtual” despite describing ITAP conversion; preserve that anomaly. Missing prices and the commercial KITAP code mapping also remain unverified. No source conflict is silently corrected.
