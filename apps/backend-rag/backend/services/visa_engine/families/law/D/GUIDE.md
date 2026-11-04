# Guide — Family D (multiple-entry visits)

Law: nine adjacent cards and `INDEX.md`, copied from the group3 extraction of 2026-10-17. Wording and page hashes are unchanged. D4/D8 only say `Data Belum Tersedia`: keep their cards, project `TO_VERIFY` with NULL permit class and exclude them from every Guide route. No ITAP file is supplied for this family.

```text
Q1 Main purpose of repeated visits?
   Tourism / family / participation in events / transit .... D1
   Business meetings / negotiations / purchasing ........... D2
   Medical treatment ...................................... D3
   Arts/cultural performance (no Indonesian employment) .... D7
   Pre-investment field surveys / feasibility studies ...... D12
   Film / music video / TV / radio production .............. D14
   Audit / quality control / inspect a company branch ...... D17
     Q2 Days per entry
        (no sponsor question: Bali Zero sponsors, owner ruling 2026-10-19)
```

Every path has two questions. D1/D2's **nationality sponsor exception** stays in the Law cards, but the Guide no longer asks about it (agency-sponsor ruling below). No exception-country list is invented. Unknown/invalid answers ask again. Non-national-document return permission and tickets remain checklist items; the cards do not turn them into a sponsor condition.

| Code             | Initial stay per entry | Extension / total per entry                                       |
| ---------------- | ---------------------- | ----------------------------------------------------------------- |
| D1/D2/D3/D14/D17 | 60 DAY                 | Extendable up to 180 days.                                        |
| D7               | 30 DAY                 | Not extendable; maximum 30 days.                                  |
| D12              | 180 DAY                | Owner ruling: maximum 180 days per entry (`max_total_days: 180`). |

Duration is a positive whole number of **days per entry**. Initial options remain DAY. A visa valid for one, two or five years is not permission for an uninterrupted stay of that length, and those validity terms never become YEAR stay options. Up to the initial stay returns `OK`; within a known extension ceiling, `OK_WITH_EXTENSION` and `total_max_known: true`; above it, `STAY_TOO_LONG`.

**Owner ruling, 2026-10-19 (not verbatim Law): D1 and D2 with one-year validity allow 60 days per entry, extended up to two times by 60 days per entry, 180 days total.** The official cards say `beberapa kali hingga maksimal 180 hari`; their text is untouched. This ruling's extension count is not silently extended to other validity terms or codes.

D12's `jenis_visa_verbatim` says extension for another 180 days, while `total_max_verbatim` says one extension with a total of 12 months or two years depending on the chosen visa duration. Both passages stay quoted in the card. **Owner ruling, 2026-10-19 (not verbatim Law): D12's maximum stay is 180 days per entry; the visa is valid 1 or 2 years.** `max_total_days` is 180 (per entry) and `max_total_months` stays NULL. Up to 180 days the Guide returns `OK` with the `180 DAY` option; above 180 days per entry it returns `STAY_TOO_LONG`. The ruling is also in the D12 card's `owner_rulings`. NB-2 reads Permenkumham 22/2023 jo. 11/2024 Pasal 95 ayat (4) as 180-day extensions up to 12 months total; that reading is recorded as PLAUSIBLE dissent and the owner ruling decides.

| Guide condition           | Verbatim Law location                                                                                                     |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Activity                  | Each `definition_verbatim`; D2 also prohibits continuous supervision of production/sales in `ketentuan_lain_verbatim`.    |
| D1/D2 conditional sponsor | `sponsor_required.verbatim`: no sponsor, except nationals in the linked list. Not asked: agency-sponsor ruling.           |
| D3/D7/D14/D17 sponsor     | `sponsor_required`: `Anda membutuhkan penjamin/sponsor`. Not asked: agency-sponsor ruling.                                |
| D12 sponsor               | `sponsor_required`: `Anda tidak membutuhkan penjamin/sponsor`.                                                            |
| Stay / extension          | `stay_days`, `extendable`, `total_max_verbatim`. D12's two inconsistent passages stay quoted; the 180-day ruling decides. |

Documents remain the complete `requirements_verbatim` checklist, including passport, funds, photograph, CV and itinerary where stated, application/sponsor/relationship support, and these special requirements: D1/D2 institutional correspondence **or** Indonesian spouse/parent family support; D3 medical-purpose statement; D7 event invitation **or** impresariat request and performer agreement for music/support roles; D12 institutional correspondence; D14 location/filming authorisation; D17 organiser invitation. Non-national-document holders retain the return-entry-permission and onward-ticket conditions and crew exception. No document possession becomes a question.

D7 explicitly permits remuneration or facilities for its activity while forbidding an employment relationship with Indonesian individuals or corporations. Other D cards prohibit sales/services and Indonesian remuneration as printed. Do not replace D7's rule with an unpaid-only condition. All substantive D pages prohibit conversion to ITAS; no ITAP stage is inferred and DB `itap` remains NULL.

Exact price keys, all in `multiple_entry_visas`: `D1 Tourism (1 Year)`, `(2 Years)`, `(5 Years)`; `D2 Business (1 Year)`, `(2 Years)`, `(5 Years)`; `D12 Business Investigation (1 Year)`, `(2 Years)`. These are catalog references, not a duration-specific quote. D3/D7/D14/D17 and secondary D4/D8 have no exact keys: `price_keys=[]`, `is_sold=NULL`. PNBP is reference only.

TO_VERIFY / ambiguities: D4/D8 missing content and all legal fields; application location for all substantive codes; the absent D1/D2 sponsor-exception country list and D1/D2/D12's no-sponsor application wording versus extension instructions naming a sponsor (both moot for the Guide under the agency-sponsor ruling, kept as Law history). `apply_channel` stays NULL throughout. Fidelity `VERIFIED` for substantive cards does not settle these procedural conflicts. There is no schema change.

**OWNER RULING, 2026-10-19 (not verbatim Law): agency sponsor.** Bali Zero is an agency and can always sponsor. `family_d.json` sets `agency_sponsors: true`: the Guide never asks a sponsor or sponsor-exception question for any D code and never returns `NEEDS_SPONSOR`. The `travel_document` and `sponsor` questions were removed from the family config. The DB `sponsor` column keeps the Law value (`CONDITIONAL` for D1/D2, `REQUIRED` for D3/D7/D14/D17, `NOT_REQUIRED` for D12); the ruling is a Guide behaviour, not a change to the Law.
