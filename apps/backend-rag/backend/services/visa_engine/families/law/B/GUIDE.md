# Guide — Family B (visa on arrival)

Law: B1, B4 and `INDEX.md`, copied from the group3 extraction of 2026-10-17. Source wording, URLs and `page_sha256` are unchanged. No ITAP file exists for this family in the supplied extraction.

```text
Q1 Main purpose?
   Tourism / family / meetings / events / transit .......... B1
   Official government duties ............................. B4
Q2 B1: nationality (ISO alpha-2) on the card's list? Unknown code → ask again;
   unlisted → NOT_COVERED. B4: nationality confirmed eligible?
Q3 Travel status?
   National / temporary / emergency passport → continue
   Stateless / other non-national document   → NOT_COVERED
Q4 Days needed?
   1–30 → OK, initial 30 DAY option
   31–60 → OK_WITH_EXTENSION, total_max_known true
   61+ → STAY_TOO_LONG
```

No path exceeds four questions. Both codes require no sponsor. Missing, unknown or invalid choices ask again; a failed eligibility condition returns `NOT_COVERED`. Duration is a positive whole number of days. DAY options are never converted to months or years. Owner ruling 2026-10-19: **B1 is a 30-day initial product**. Its extension is distinct, not an initial 60-day stay.

| Guide condition      | Verbatim Law location                                                                                                                                                        |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Purpose              | B1/B4 `definition_verbatim`. Government work takes B4, not a tourist label merely because incidental tourism is allowed.                                                     |
| Eligible nationality | `jenis_visa_verbatim`: `bagi negara subjek visa kunjungan saat kedatangan`. B1: the card's portal-sourced `eligible_nationalities` (owner ruling below). B4: no list.        |
| Travel status        | `ketentuan_lain_verbatim`: temporary/emergency passports expressly allowed; stateless persons, titre du voyage, certificate of identity, laissez passer or similar excluded. |
| Stay / extension     | `stay_days` and `total_max_verbatim`: initial 30 days, one extension up to 60 total.                                                                                         |

Documents remain the complete route-specific Law checklist: passport biodata and validity (six months, twelve months for the stated temporary/emergency or other travel-document case), recent colour photograph, return/onward ticket with the crew exception, application, payment and visa/entry steps. B4's checklist uses broader non-national-document examples than its eligibility paragraph: this does not override the explicit exclusion. No upload or possession question is added.

B1 links exact keys `single_entry_visas` / `B1 Visa on Arrival (VOA)` and `B1 Visa on Arrival Extension`. B4 has no exact key: `price_keys=[]`, `is_sold=NULL`. Government PNBP is reference only. The e-VOA's 90-day validity is not a stay option. Pre-arrival versus arrival payment routes are not converted into an offshore/onshore classification: DB `apply_channel` stays NULL.

Both cards mention conversion through bridging; they do not establish a direct ITAP entitlement. DB `itap` stays NULL. TO_VERIFY / ambiguities: the absent B4 nationality list; whether the M.HH-02.GR.01.06/2024 annex equals the 97-entry B1 portal list; B4's broad checklist examples versus its explicit other-document exclusion; the exact conversion procedure is not assessed here. Neither supplied page is NOT_FOUND. `VERIFIED` means faithful extraction, not a guarantee of nationality or conversion eligibility.

**OWNER RULING, 2026-10-19 (not verbatim Law): B1 nationality list.** The B1 card now carries `eligible_nationalities`: the 97 entries of the official portal page https://www.imigrasi.go.id/wna/daftar-negara-voa-bvk-calling-visa/daftar-negara-subjek-visa-on-arrival, retrieved 2026-10-19, with `authority_type: OFFICIAL_PORTAL`. The page cites no instrument. The card records it as Kepmenkumham M.HH-02.GR.01.06 Tahun 2024 (ditetapkan 9 January 2024, revoking M.HH-04.GR.01.06/2023), seen in a secondary source (bengkalis.imigrasi.go.id, page of 2024-01-16); that its annex equals today's 97 entries is TO_VERIFY. The field is separate from the page verbatim (`not_page_verbatim: true`). The B1 nationality question takes an ISO 3166-1 alpha-2 code: unknown → asked again; listed → continue; unlisted → `NOT_COVERED`. 18 of the 19 A1 nationalities are on this list and stay B1-eligible: A1 eligibility never excludes B1. B4 keeps its self-confirmation question.
