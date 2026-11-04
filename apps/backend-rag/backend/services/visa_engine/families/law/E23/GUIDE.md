# Guide — Family E23 (Work)

Law: the six adjacent cards, `INDEX.md` and `ITAP.json` from the supplied group3 extraction of 2026-10-17. All source fields, quotations and page hashes are retained. The only added card fields are `itap_eligibility_as_stated_boolean` and `itap_status`, copied from that code's assessment in `ITAP.json`.

```text
Q1  What will you do in Indonesia?
    Work in Indonesia ........................................ E23
      Q2 Sponsor in Indonesia who will provide your work?
         No ........................................ NEEDS_SPONSOR
         Yes → Q3 Which initial stay do you need?
                  180 days ........................... 180 DAY, OK
                  1 year .............................. 1 YEAR, OK
                  2 years ............................. 2 YEAR, OK
```

Three questions on every successful path. Missing or invalid answers ask the same question again. Only the work activity routes to E23; unknown activities and secondary codes do not fall back to generic E23. This family does not determine which other visa covers an unsupported activity.

| Guide condition        | Verbatim Law location                                                                                                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Work activity          | `definition_verbatim`: `bekerja dengan penjamin di Indonesia`                                                                                                                                      |
| Sponsor providing work | `sponsor_required.verbatim`: `Anda membutuhkan penjamin/sponsor untuk mengajukan visa ini.` Requirement 1: `surat permohonan dan surat pernyataan penjamin yang memberikan kerja bagi Orang Asing` |
| Initial stay           | `stay_days`: `Anda dapat tinggal di Indonesia selama 180 hari, 1 tahun, atau 2 tahun, sesuai dengan pilihan Anda. Izin tinggal dihitung sejak tanggal kedatangan.`                                 |

The initial stay is a discrete choice in its published unit. `stay_question` is an optional resolver capability needed because the existing month-based calculation excludes DAY options. It selects only a member of the code's `stay_options`; no DAY/MONTH/YEAR conversion occurs. A numeric `stay_days` or `stay_months` answer alone cannot select a permit. The answer describes the initial permit selected, not approval of a separately supplied longer requested stay.

Extension is expressly allowed, online through the sponsor. The code page states no extension increment, renewal count or cumulative maximum. Both total maxima remain NULL. No open-ended extension outcome or invented duration ceiling is offered by this initial-permit Guide. The visa's 90-day entry validity is separate from its stay, which starts on arrival. Multiple entry applies while the re-entry permit remains valid.

Documents remain the complete seven-item `requirements_verbatim` checklist: application and work-provider sponsor statement, passport valid for at least six months, the last three months' bank statements in the foreign national's or sponsor's name for at least USD 2,000 or equivalent, recent photograph, CV, itinerary, and `Izin bekerja dari Kementerian Tenaga Kerja.` No question asks whether the traveller holds or has uploaded a document. `OK` identifies the initial permit route; it does not verify that checklist or grant unrestricted work rights. The complete restrictions, including the selling exception `kecuali diperlukan dalam pekerjaan tersebut`, remain verbatim in the card.

E23A/U/V/X/Y return HTTP 200 with an official title and `Data Belum Tersedia`. They are secondary cards, projected as `TO_VERIFY` with NULL permit class, sponsor, entry, stays, extension and sales status, and empty price keys. They have no Guide routes. Their titles alone do not authorize SEZ, diplomatic-household, trade-office, government-expert or digital-expert eligibility. The legacy seed's `E23-FREELANCE` has no official card and is not introduced as a visa code.

Only these exact `kitas_permits` references are linked to E23:

- `Working KITAS (Altus/Onshore)`
- `Working KITAS (Offshore)`
- `Working KITAS (Extend)`
- `Freelance E23 (Altus/Onshore)`
- `Freelance E23 (Offshore)`

These are commercial references, not new legal conditions or duration-specific quotations. Working initial products say one year; Freelance products say six months. Six months is not silently equated to 180 days. No two-year price or unsponsored freelance permission is invented. The code's PNBP stays reference-only. Offshore/onshore key labels do not establish legal application location; `apply_channel` stays NULL.

ITAP is a later stage inside this family. `ITAP.json` retains the general worker-category conversion text and the three-year condition verbatim, but explicitly declines to map categories to E23 codes. Every code therefore has `itap_eligibility_as_stated_boolean: null` and `itap_status: TO_VERIFY`. E23 retains only its generic conversion quotation as `itap_verbatim`; the other five retain NULL. DB `itap` stays NULL throughout. No ITAP question or KITAP commercial key is introduced.

TO_VERIFY: every unstated field on E23A/U/V/X/Y; E23's cumulative maximum, extension increments and renewal count; application location for all six codes; per-code ITAP eligibility and conditions for all six; commercial six-month versus legal 180-day duration correspondence; absence of an exact two-year price. `VERIFIED_FROM_SOURCE` establishes extraction fidelity only. No owner ruling about C1, B1, D1 or D2 is reclassified as E23 law or changed here.
