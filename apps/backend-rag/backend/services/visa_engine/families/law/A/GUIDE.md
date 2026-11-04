# Guide — Family A (visa exemptions)

Law: four adjacent code cards and `INDEX.md`, copied from the group3 extraction of 2026-10-17. Wording, source URLs and `page_sha256` are unchanged. No ITAP file exists for this family in the supplied extraction.

```text
Q1 Main purpose?
   Tourism / family / meetings / events / transit .......... A1
     Q2 Nationality (ISO alpha-2)? Listed → Q3; unknown code → ask again;
        unlisted → Q2b Singapore permit holder? Yes → TO_VERIFY; No → NOT_COVERED
     Q3 National passport (not stateless/non-national document)?
     Q4 Days needed → 1–30 OK; 31+ STAY_TOO_LONG

   Official government duties ............................. A4
     Q2 Nationality confirmed eligible for this exemption?
     Q3 National passport (not stateless/non-national document)?
     Q4 Days needed → 1–30 OK; 31+ STAY_TOO_LONG

   Active captain / pilot / transport crew ................. A36
   Captain / crew / foreign expert arriving directly aboard
   a vessel or floating equipment to operate in Indonesian
   waters, territorial sea, continental shelf or EEZ ........ A37
     Q2 National passport (not stateless/non-national document)?
     Q3 Days needed → 1–30 OK; 31+ STAY_TOO_LONG
```

Each path asks at most four questions, except an unlisted A1 nationality, which adds the Singapore permit-holder question. A failed nationality or travel-status condition returns `NOT_COVERED`; a missing, unknown or invalid answer asks again. All codes require no sponsor, have one entry, an initial `30 DAY` stay and no extension. The selected stay option retains DAY. Duration accepts only a positive whole number of days. No conversion entitlement is inferred.

| Guide condition                             | Verbatim Law location                                                                                                                                                                                                                                                                                                           |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Purpose                                     | Each code's `definition_verbatim`; A37 states arrival directly with the vessel and its operating waters.                                                                                                                                                                                                                        |
| Eligible nationality, A1/A4 only            | `jenis_visa_verbatim`: `diperuntukkan bagi warga negara tertentu`. A1: decided from the card's sourced `eligible_nationalities` list (owner ruling below). A4: no country list is supplied; eligibility must be confirmed from the official list before answering yes. No nationality list is invented or inherited by A36/A37. |
| Stateless / non-national-document exclusion | `ketentuan_lain_verbatim`: `tidak diizinkan mengajukan fasilitas bebas visa`. This is a legal eligibility distinction, not a document-possession question.                                                                                                                                                                      |
| Initial and total stay                      | `stay_days`, `extendable`, `total_max_verbatim`: maximum 30 days, no extension.                                                                                                                                                                                                                                                 |

Documents and application steps remain the full `requirements_verbatim` checklist: passport valid at least six months; return/onward ticket with the stated crew exception; immigration entry endorsement; electronic application/entry record where applicable. A36/A37 also retain registration in the general declaration or crew list, with the exact private-vehicle/cargo wording. None becomes a question. Selling goods/services and Indonesian remuneration remain prohibited as printed in each card.

Only `single_entry_visas` / `A1 Visa-Free Tourism` has an exact catalog key. A1 `is_sold` is true; A4/A36/A37 have `price_keys=[]`, `is_sold=NULL`. Official zero PNBP is reference only. DB `apply_channel` and `itap` remain NULL: arrival/pre-arrival routes do not establish an offshore/onshore classification or an ITAP stage.

TO_VERIFY / ambiguities: the nationality list is absent from the A4 card; A1's Singapore permit-holder route (Perpres 95/2024 Lampiran B versus the 10/2026 title, see the card); A36 and A37 can overlap; A37's definition includes foreign experts and specific waters while its generic visa-type paragraph names captains/pilots/crew. Keep both paragraphs without widening the latter. No supplied card is NOT_FOUND; fidelity `VERIFIED` does not resolve eligibility or these wording differences.

**OWNER RULING, 2026-10-19 (not verbatim Law): A1 nationality list.** The A1 card now carries `eligible_nationalities`, a regulation-sourced field kept separate from the page verbatim (`not_page_verbatim: true`), retrieved 2026-10-19:

- 13 states/SARs from Perpres 95/2024 Lampiran A (https://peraturan.go.id/files/perpres-no-95-tahun-2024.pdf): BN, PH, KH, LA, MY, MM, SG, TH, VN, TL, SR, CO, HK;
- 6 added by Permenimipas 10/2026 Lampiran (https://peraturan.bpk.go.id/Download/416328/Permen_Nomor10_Tahun2026.pdf): TR, BR, PE, KZ, MO, BY;
- the non-nationality route of Perpres 95/2024 Lampiran B, `Pemegang izin tinggal tertentu dari negara Singapura` (`permit_holder_route`).

The official BVK portal page shows the same 19. The Guide's A1 nationality question takes an ISO 3166-1 alpha-2 code: an unknown code is asked again; a listed code passes; an unlisted code is asked about the Singapore permit-holder route: yes returns `TO_VERIFY` (Permenimipas 10/2026 dropped the permit-holder wording, and NB-2 reads Lampiran B as applying only at certain Kepulauan Riau entry points), no returns `NOT_COVERED`. 18 of the 19 (all except MO) are also on the B1 list; family B never excludes them. A4 keeps its self-confirmation question: no A4 list was ruled on.
