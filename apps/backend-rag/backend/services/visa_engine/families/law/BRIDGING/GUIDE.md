# Guide — BRIDGING (Izin Tinggal Peralihan)

Law: [BRIDGING.json](BRIDGING.json), the unchanged group3 extraction of 2026-10-17. [INDEX.md](INDEX.md) preserves its source findings. The main source is an official Depok republication of the 23 April 2024 announcement. The central press URL returned 404; its failed retrieval is recorded alongside the successful sources. This is a stay permit, not an entry entitlement or an E-family ITAP stage.

```text
Q1 Which transition between stay permits do you need?
   eVisa-applied visit stay permit → ITAS
   ITAS that can no longer be extended → new stay permit
   ITAP that can no longer be extended → new stay permit
   (missing/unlisted answer → ask Q1 again)
     Q2 Already in Indonesia, remaining there while using this permit?
        no → NOT_COVERED
        yes → Q3 Apply through eVisa AND pay no later than
                 3 days before the current stay permit expires?
                 no → NOT_COVERED
                 yes → BRIDGING, TO_VERIFY (nationwide sponsor rule unknown)
                       published validity: 60 DAY; ON SHORE only
```

Each path has three questions. No `OK`, extension, single-entry or re-entry entitlement is inferred. `TO_VERIFY` means the known routing conditions match but sponsor assessment is unresolved; it is not approval. The published 60-day validity is stored as `initial_stay_days: 60` and `stay_options: [{value: 60, unit: DAY}]`. It supplies neither a cumulative maximum nor an extension rule. Duration is not another question because there is only one stated validity period.

## Every question comes from the card

Q1 uses `allowed_activities` / `ketentuan_lain_verbatim[0]`, including these exact clauses:

> warga negara asing pemegang Izin Tinggal Kunjungan yang diajukan melalui evisa.imigrasi.go.id dimungkinkan untuk memperoleh Izin Tinggal Terbatas tanpa harus keluar wilayah Indonesia.

> Begitu juga pemegang Izin Tinggal Terbatas dan Izin Tinggal Tetap yang sudah tidak bisa lagi diperpanjang, dapat memperoleh Izin Tinggal baru tanpa harus keluar wilayah Indonesia

The first route explicitly leads to ITAS. The other two say only a new stay permit; the following sentence discusses conversion to ITAS. Whether that narrows every destination remains TO_VERIFY, and the traveller's eligibility for the destination is not established here. ITAP is an origin in that quotation, not a promised destination or a new family. No B1/visa-on-arrival, visa-free or arbitrary visit-permit route is added by inference.

Q2 uses `where_to_apply`, `stay_days[0]` and `prohibited_or_conditions_verbatim[0]`:

> Masa berlaku Izin Tinggal Peralihan yakni 60 hari dan hanya berlaku secara onshore, yakni bagi WNA yang sudah berada di wilayah Indonesia.

> Izin tinggal ini tidak berlaku lagi apabila WNA keluar wilayah Indonesia.

Q3 uses `requirements_verbatim[0]` / `pengajuan_intro_verbatim`:

> Warga negara asing yang ingin menggunakan Izin Tinggal Peralihan harus mengajukan permohonan melalui laman evisa.imigrasi.go.id dan melakukan pembayaran biaya keimigrasian paling lambat 3 (tiga) hari sebelum masa berlaku izin tinggal sebelumnya habis.

The requirement concerns both application and payment. The separate statement about approval after expiry remains verbatim in the card; it does not waive this advance deadline or authorize a late application.

## Checklist, price and unknowns

All nine `requirements_verbatim` items remain a checklist. The eight Jakarta Pusat items preserve their source URL, local-office scope and original spelling: sponsor account, application letter, stamped guarantee, sponsor identity, passport/photo, last permit, Form E02 and the conditional power of attorney. The Guide asks no questions about possessing these documents.

The nationwide sponsor Boolean remains NULL. Local sponsor-account and guarantee requirements do not establish a nationwide exemption or blanket rule. After the three routing answers the resolver therefore returns `TO_VERIFY` without asking C-family travel-document questions.

The only price reference is the exact catalog key `single_entry_visas` / `Bridging Visa`; `is_sold` is true. No amount is stored in the family or migration outside the unchanged Law card. The category of that commercial key does not imply single entry. PNBP is unknown; Rp 10,000 stamp duties in the local checklist are neither PNBP nor client prices.

TO_VERIFY: aggregate stay maximum; entry entitlement; nationwide sponsor requirement; PNBP amount; applicability of the Jakarta Pusat checklist elsewhere; unspecified destination class for ITAS/ITAP-origin transitions. The card is `VERIFIED_FROM_SOURCE` and the row `VERIFIED` for extraction fidelity, with those legal/procedural unknowns intact. There are no NOT_FOUND secondary cards in this family and no supplied `ITAP.json`. DB `itap`, validity dates and successor stay-permit code remain NULL. No schema change is needed.

**OWNER RULING, 2026-10-19: BRIDGING is not extendable** (`extendable: false`). Basis, Permenkumham 11/2024 angka 23, Pasal 86A ayat (1) (https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf, retrieved 2026-10-19):

> Izin Tinggal Kunjungan bagi Orang Asing dalam rangka peralihan Izin Tinggal Keimigrasian sebagaimana dimaksud dalam Pasal 80 ayat (3) huruf f dan ayat (4) huruf d diberikan untuk jangka waktu paling lama 60 (enam puluh) hari dan tidak dapat diperpanjang.

The quote sits in the card's `regulation_verbatim`, separate from the page extraction, whose own `extendable` field stays NULL. The card's former `extendable: null + TO_VERIFY` entry is resolved and recorded in `owner_rulings.history`. The Guide is unchanged: it still returns `TO_VERIFY` for the unresolved sponsor rule.
