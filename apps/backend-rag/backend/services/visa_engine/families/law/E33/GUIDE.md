# Guide — Family E33 (Second Home)

Law: the eight adjacent code cards and `ITAP.json`, copied from the canonical group1 extraction of 2026-10-17. Their wording and page hashes are unchanged. E33D has an official title but only “Data Belum Tersedia”: keep its card, project `TO_VERIFY` with NULL permit class, and exclude it from the Guide.

```text
Q1  Which situation describes your intended stay?
    Settle using an immigration guarantee ...................... E33
      Q2 Commit to either USD 130,000 in your own state-bank
         account or an Indonesian flat/apartment
         worth at least USD 1,000,000?
      Q3 Months needed → 5 YEAR; extension within 120 months total

    Government-invited expert, central-government sponsor ...... E33A
    Government-invited world figure, central-government sponsor  E33C
      Q2 Sponsor = central government? Otherwise NEEDS_SPONSOR
      Q3 Months needed → 5 or 10 YEAR; maximum 120 months total

    Government-invited expertise collaboration ................. E33B
      Q2 Commit to proof of collaboration with the government
         or a state institution?
      Q3 Months needed → 5 or 10 YEAR; maximum 120 months total

    Senior residence, initial stay up to 5 years ................ E33E
      Q2 Aged at least 55?
      Q3 Commit USD 50,000 in your own state-bank account?
      Q4 Income or allowance at least USD 3,000/month?
      → 5 YEAR (the only initial choice on this code's page)

    Senior residence, initial stay 1 year ...................... E33F
      Q2 Aged at least 55? (owner ruling 2026-10-19)
      Q3 Income or allowance at least USD 3,000/month?
      Q4 Have a sponsor? Otherwise NEEDS_SPONSOR
      Q5 Months needed → 1 YEAR; extension within 72 months total

    Tasks under an employment agreement with a foreign company  E33G
      Q2 Salary or income at least USD 60,000/year?
      Q3 Months needed → 1 YEAR; extension within 72 months total
```

No path asks more than four questions, except E33F (five, since the 2026-10-19 age ruling). A failed commitment, age or income condition returns `NOT_COVERED`; it does not silently switch to another product. The activity option is an assertion of the situation printed in its `labels_en`, including the government invitation or foreign-company employment relationship. It is not a question about holding a document.

Duration is a positive whole number of months. YEAR compares as exactly 12 MONTHS; initial options retain YEAR units and are never converted into days. The smallest fitting initial option returns `OK`. Beyond the initial option but within a stated cumulative maximum returns `OK_WITH_EXTENSION`, the largest initial option and `total_max_known: true`. Beyond the cumulative maximum returns `STAY_TOO_LONG`. A maximum is a ceiling, not a guarantee of renewal.

E33E selects its sole 5 YEAR initial option after its three conditions, without a fifth question. This result describes the initial permit; it does not assess a requested longer stay. If `stay_months` is explicitly supplied, the same duration validation applies: 61–120 months need extension, above 120 is too long, and invalid values ask again. Missing or invalid answers to every other question ask that question again.

| Guide condition                                     | Verbatim Law location                                                                                                                                                                                                              |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Purpose / invitation / foreign-company relationship | Each `definition_verbatim` and official title. E33F's title says `Lansia`; its body states activities rather than a numeric age. E33G also requires `Perjanjian kerja dengan perusahaan yang didirikan di luar Wilayah Indonesia.` |
| E33 immigration guarantee                           | `requirements_verbatim` 7–8: own-name state-bank funds at least `US$130.000` **or** an Indonesian `rumah susun atau apartemen` at least `US$1.000.000`. The commitment is prospective.                                             |
| E33A/C sponsor                                      | Definition plus requirements: `pemerintah pusat` / `instansi pemerintah pusat`. A local government, company or individual does not meet this condition.                                                                            |
| E33B collaboration commitment                       | Special requirement 8: `Surat Pernyataan Komitmen akan menyampaikan bukti kerja sama dengan pemerintah atau lembaga negara`.                                                                                                       |
| E33E age                                            | Definition: `berusia 55 tahun atau lebih`.                                                                                                                                                                                         |
| E33E deposit commitment                             | Special requirement 8: own account at a state-owned bank, at least `US$50.000`.                                                                                                                                                    |
| E33E/F income                                       | Special requirements: `penghasilan atau tunjangan` at least `US$3.000` per month; no passive-income-only restriction is added.                                                                                                     |
| E33F sponsor                                        | `sponsor_required`: `Anda membutuhkan penjamin/sponsor`. The page does not specify sponsor type.                                                                                                                                   |
| E33G annual income                                  | Special requirement 6: `gaji atau penghasilan` at least `US$60.000 per tahun`.                                                                                                                                                     |
| Initial stays                                       | Code-page `stay_days`: E33/E33E up to 5 years; A/B/C 5 or 10; F/G 1.                                                                                                                                                               |
| Cumulative stays                                    | Code-page `total_max_stay_verbatim`: E33/A/B/C/E, 10 years. F/G: the separate, hash-pinned `supplementary_stay_evidence.extension_verbatim` explicitly names the code and caps the total at 6 years.                               |

Documents remain the complete `requirements_verbatim` checklist: passport, bank statements, photograph, CV, itinerary, invitation, sponsorship evidence, collaboration evidence, qualifications and employment agreement as applicable. E33B retains the alternative of a needed expertise certificate **or** graduation from a listed university within three years with GPA at least 3.5. The reference directory is preserved; its “100” heading has 109 entries. Neither document possession nor uploading a document becomes a question.

E33/E33B/E33E commitments must be fulfilled and reported within the page's stated 90-day period. Proof, reporting, ongoing guarantee maintenance, and the full conduct restrictions (including the selling exception `terkecuali diperlukan dalam pekerjaannya`) remain in the cards. E33's page starts the commitment deadline at entry, while the existing commercial wording mentions post-grant: this Guide does not resolve that conflict or invent a different clock.

Only exact price-list keys are linked: E33 (5 Years); E33E (5 Years, Extend); E33F (1 Year Offshore, 1 Year Altus/Onshore, Extend); E33G (Altus/Onshore, Offshore, Extend). All are in `kitas_permits`. Government PNBP is reference only. E33A/B/C/D have no exact commercial keys: `is_sold` remains NULL. No generic Retirement or KITAP key is assigned to an unverified stage. Commercial offshore/onshore key labels do not establish `apply_channel`, which remains NULL.

ITAP is a stage, never another family. Owner ruling 2026-10-18 “segui la legge” (replaces the earlier “E33 has no KITAP stage”): by Permenkumham 22/2023 Pasal 173 huruf f (rumah kedua, keahlian khusus, tokoh dunia, lanjut usia; 55 years or more after Permenkumham 11/2024), the ITAS may be converted to ITAP. Per code: E33 (angka 1), E33A and E33B (angka 2), E33C (angka 3), E33E and E33F (angka 4) are ITAP-eligible, recorded in each card as `itap_eligibility_as_stated_boolean: true`; E33G (remote worker, ITAS at most 1 year, absent from Pasal 173) is `false`; E33D is `null + TO_VERIFY`. `itap_status` is `LAW_MAPPED_BY_DEFINITION` (E33F: `LAW_MAPPED_BY_TITLE`) because the law lists activities and the code-to-activity link comes from the official name. Conditions: at least 3 consecutive years in Indonesia from the grant of the ITAS (Pasal 179 ayat (1)) and commitments fulfilled (Pasal 178). [ITAP.json](ITAP.json) keeps all reviewed official sections and the eight assessments. DB `itap` stays NULL for every code, as in the other E families, and no ITAP question is offered.

TO_VERIFY: E33D's missing body and legal fields; (resolved 2026-10-19 by owner ruling, kept as history: the senior age for E33E/E33F, where Permenkumham 11/2024 sets 55 in Pasal 173 f angka 4, but its restated Pasal 105 ayat (10) huruf d still says 60, as does the imigrasi.go.id ITAP page; Pasal 33 ayat (2) huruf j angka 4 also writes “55 (lima puluh)”, numeral and words disagreeing, while Pasal 173 f angka 4 reads “55 (lima puluh lima)”); application location for all codes; E33D's ITAP status; the code-to-Pasal 173 mapping is by official code name; E33's sponsor/application inconsistency and commitment deadline start; E33/E33E's code-page 5-year initial stay versus the general table's 5/10 years; E33B's directory heading/count; E33F's unstated sponsor type. F/G's code pages omit the cumulative limit; the separately pinned general table supplies the explicit code-specific six-year ceiling used here. E33C requires a sponsor although its extension instructions name the foreign national; no sponsor exemption is inferred. Fidelity status `VERIFIED` does not resolve these legal or procedural ambiguities.

**OWNER RULING, 2026-10-19 (not verbatim Law): senior age threshold 55.** For E33E and E33F the threshold is **55 per owner ruling + Pasal 173 f angka 4 (Permenkumham 11/2024)**. The cards' `senior_age_threshold_55_vs_60` TO_VERIFY (and E33F's `minimum_age_not_stated`) moves to `owner_rulings`; the 55-vs-60 conflict notes (restated Pasal 105 ayat (10) huruf d and the imigrasi.go.id ITAP page say 60) stay in the cards and `ITAP.json` as history. E33F now asks the same `age_55` question as E33E. No verbatim quote is changed.
