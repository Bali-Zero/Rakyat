# Guide — Family E31 (family / dependent)

Law: ten adjacent `<CODE>.json` cards, captured on 2026-10-17. E31 is a secondary `NOT_FOUND` card (`Data Belum Tersedia`): keep its row `TO_VERIFY`, `permit_class` NULL and exclude it from the Guide. The other nine cards contain substantive official text.

```text
Q1  Which family relationship brings you to Indonesia?
    Spouse of an Indonesian citizen ................................ E31A
    Spouse of an ITAS/ITAP holder .................................. E31B
    Child of a lawful foreign/Indonesian marriage .................. E31C
    Foreign stepchild in a lawful foreign/Indonesian marriage ...... E31D
    Child of an ITAS/ITAP or valid limited-stay visa holder ......... E31E
    Child with an Indonesian parent; court-established legal tie ... E31F
    Parent of an Indonesian child .................................. E31G
    Parent of a child with ITAS/ITAP or valid limited-stay visa ..... E31H
    Child joining a sibling with ITAS/ITAP or valid limited-stay visa E31J

Q2  E31E/H only: Are you joining a family member who does not hold a Golden Visa?
    no → NOT_COVERED
    yes → continue

Q3  Has the sponsor or family member required by this visa agreed to support your application?
    no → NEEDS_SPONSOR
    yes → continue

Q4  How many months do you need to stay? (positive whole number)
    1–12  → OK + stay_option {value: 1, unit: YEAR}
    13–24 → OK + stay_option {value: 2, unit: YEAR}
    >24   → OK_WITH_EXTENSION + stay_option {value: 2, unit: YEAR}
            + total_max_known: false
```

Three questions on most paths; four on E31E/H. Missing or invalid answers repeat the same question. YEAR is exactly 12 MONTHS for comparison; the stored initial options stay in YEAR, never days. The pages permit extensions but publish no cumulative maximum: the extension verdict above 24 months does not promise that the requested total will be granted.

| Question              | Condition in the verbatim Law                                                                                                                                                                                                                                                                                                                                                             |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Family relationship   | Each `official_name_id` states the relevant spouse, child, parent or sibling relationship. E31C explicitly names a lawful WNA–WNI marriage; E31D names its foreign stepchild. E31F's `persyaratan_khusus_verbatim` requires an Indonesian court determination explaining the legal parent-child relationship. This is the legal circumstance, not a question about possessing a document. |
| Golden Visa exclusion | Only E31E/H `prohibited_or_conditions_verbatim` expressly disallow joining a Golden Visa holder as a Family Dependant. No equivalent gate is inferred for E31B/J or the other codes.                                                                                                                                                                                                      |
| Sponsor               | Every substantive card has `sponsor_required.as_stated_boolean: true`. The selected card's family application request, guarantee or family commitment remains in its checklist.                                                                                                                                                                                                           |
| Duration              | Every substantive `stay_days` states 1 or 2 years from arrival; `extendable` permits extension without a cumulative maximum.                                                                                                                                                                                                                                                              |

The relationship options identify the applicant's relationship to the person being joined. E31C and E31F are not treated as synonyms. The source does not publish an exhaustive rule separating all cases: a broad “child of WNI” answer is insufficient and repeats Q1. The court-established legal relationship identifies E31F's stated circumstance; the Guide does not infer adoption, an age threshold, unmarried status or that mixed-marriage and court-established cases can never overlap. Overlapping C/F cases remain `TO_VERIFY` for route applicability.

The sponsorship answer means support required by the selected card, with its exact evidence shown after the result. E31A/C/D/F/G request an application from the specified Indonesian relative; E31E permits a WNI guarantor or the foreign parent's commitment; E31H/J permit a guarantor or the stated family commitment. E31D's title names the foreign stepchild, while the application request and KK name the Indonesian parent; do not turn that title into a foreign-sponsor rule. E31B's title names an ITAS/ITAP-holder spouse, but its application request names a WNI spouse: this contradiction remains `TO_VERIFY`, without silently replacing either sentence. A Guide verdict routes to the code's Law checklist; it does not settle these source discrepancies.

Documents are checklists, never questions: marriage/birth evidence, Indonesian family registers, court determinations, current relative permits or limited-stay visas, guarantees or commitments, passport validity, funds/bank statements, photo, CV and itinerary all remain in `requirements_verbatim` and its context. The foreign relative's still-valid limited-stay visa is an explicitly documented alternative for E31E/H/J; no extra permit-readiness question is added. Conduct and work restrictions, including qualifications and reporting requirements, remain verbatim; the Guide adds no work entitlement.

Prices are exact `kitas_permits` references only. The six `Spouse 1 Year` / `Spouse 2 Years` keys (`Altus/Onshore`, `Offshore`, `Extend`) explicitly describe spouses of Indonesian citizens or KITAS/KITAP holders, so E31A/B use them. The six corresponding `Dependent` keys describe family of a primary KITAS/KITAP holder, so E31E/H/J use them. The references do not choose a channel or quote a price for a duration. E31C/D/F/G use the same six `Dependent` keys under the 2026-10-19 owner ruling below. `Born KITAS` and `Dependent KITAP + MERP` are not mapped by guesswork. PNBP in the cards is reference material, never a client price.

ITAP is a stage within E31. [ITAP.json](ITAP.json) preserves both the family-category conversion text and the separate GCI direct-ITAP news. All ten per-code conversion values are NULL + `TO_VERIFY`; DB `itap` stays NULL for all ten. E31A/B/C's direct GCI context is retained, but does not establish ITAS-to-ITAP conversion or replace the visa pages' ITAS description. No ITAP question is offered until the code-specific route is verified.

TO_VERIFY: E31's empty page and all its legal fields; all codes' application location, cumulative extension maximum and per-code ITAS-to-ITAP conversion; E31C/F overlapping applicability; E31B spouse nationality mismatch; E31A PNBP total/components mismatch; E31A/B/C GCI versus ITAS-page applicability; Born KITAS and Dependent KITAP mappings. The complete source issue entries and nulls remain in the copied cards. No shared resolver, importer or DB schema change is required.

**OWNER RULING, 2026-10-19 (commercial, not verbatim Law): E31C, E31D, E31F and E31G are priced.** Each links exactly the six `kitas_permits` keys `Dependent 1 Year (Altus/Onshore)`, `Dependent 1 Year (Offshore)`, `Dependent 1 Year (Extend)`, `Dependent 2 Years (Altus/Onshore)`, `Dependent 2 Years (Offshore)`, `Dependent 2 Years (Extend)` from `data/bali_zero_official_prices_2026.json`, with `is_sold: true`. This supersedes the earlier ruling that left them unpriced (`price_keys: []`, `is_sold: null`). The keys are catalog references, not a choice of channel or a duration-specific quote.
