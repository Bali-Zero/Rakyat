import "server-only";

/**
 * Answer keys, point values and panel rubrics. SERVER ONLY: nothing here may
 * reach the browser, not even after the PIN unlock. Points are in TENTHS so
 * that 1.2 x 5 is exactly 60 and never 5.999999.
 *
 * Mapping from the spec's ids to the opaque ids in `./content`:
 *   m1  k1..k6 = c3 c5 c1 c6 c2 c4        (most urgent first)
 *   m4  mcq (a) = p1 ; m5 q14 (a) = t1
 *   m6  o1..o8 = q4 q7 q1 q8 q3 q6 q2 q5  (o1-o5 right, o6-o8 wrong)
 *   m8  best / acceptable / unacceptable per situation, below
 */
export const KEY = {
  m1: { order: ["c3", "c5", "c1", "c6", "c2", "c4"], max: 100 },
  m2: {
    pairs: {
      A1: "2026-12-15",
      A2: "2026-12-20",
      A3: "2026-12-31",
      A4: "2027-03-31",
      A5: "2027-04-30",
      B1: "500000",
      B2: "100000",
      B3: "1000000",
      B4: "100000",
    } as Record<string, string>,
    max: 100,
  },
  m3: {
    wrong: ["f6", "f8", "f9", "f10"],
    perHit: 30,
    perFalseTap: -15,
    max: 120,
    reasons: {
      f6: "NPWP penerima di invoice 0000 0000 0000 0002; satu digit tidak sama.",
      f8: "DPP PPh 23 adalah nilai jasa tanpa PPN: Rp 20.000.000. Rp 22.200.000 sudah termasuk PPN.",
      f9: "Jasa konsultansi 2% (15% untuk royalti, bukan jasa).",
      f10: "Seharusnya 2% x Rp 20.000.000 = Rp 400.000; angka salah karena DPP dan tarif salah.",
    } as Record<string, string>,
  },
  m4: {
    toleranceRp: 1,
    numeric: [
      {
        id: "q41ppn",
        label: "Soal 1: PPN yang benar",
        value: 1650000,
        pts: 15,
      },
      {
        id: "q41total",
        label: "Soal 1: total invoice yang benar",
        value: 16650000,
        pts: 15,
      },
      {
        id: "q42",
        label: "Soal 2: transfer ke penyedia",
        value: 21800000,
        pts: 40,
      },
      {
        id: "q43w",
        label: "Soal 3: PPh Final 4(2) dipotong",
        value: 9600000,
        pts: 15,
      },
      {
        id: "q43t",
        label: "Soal 3: ditransfer ke pemilik",
        value: 86400000,
        pts: 15,
      },
      {
        id: "q44",
        label: "Soal 4: PPh 21 terutang setahun",
        value: 6750000,
        pts: 30,
      },
    ],
    mcq: { id: "q41mcq", right: "p1", pts: 10 },
    max: 140,
  },
  m5: {
    rows: {
      r1: "cocok",
      r2: "cocok",
      r3: "selisih",
      r4: "cocok",
      r5: "normal",
      r6: "cocok",
      r7: "cocok",
      r8: "ganda",
      r9: "salah_klasifikasi",
      r10: "selisih",
      r11: "dokumen",
      r12: "normal",
    } as Record<string, string>,
    notes: {
      r3: "Invoice 5.550.000, kurang 50.000 (bukan 2% PPh 23 = 100.000). Tanyakan klien, kemungkinan biaya transfer.",
      r8: "Jumlah dan referensi sama dengan r7; kelebihan bayar/uang muka pelanggan, bukan pendapatan.",
      r9: "Setoran PPN Masa 09 melunasi kewajiban pajak, bukan biaya operasional; tidak boleh dicatat sebagai Biaya lain-lain, melainkan sebagai pelunasan utang/pajak dibayar di muka PPN.",
      r10: "Invoice 33.300.000, kurang 600.000 = 2% x 30.000.000 (PPh 23 dipotong pelanggan). Minta bukti potong.",
      r11: "Tidak ada invoice atau referensi; jangan dicatat penjualan tanpa dasar.",
    } as Record<string, string>,
    q13: "INV-108",
    q14: "t1",
    ptsPerRow: 10,
    ptsPerQuestion: 10,
    max: 140,
  },
  m6: {
    right: ["q1", "q3", "q4", "q7", "q8"],
    wrong: ["q2", "q5", "q6"],
    perRight: 12,
    perWrong: -12,
    autoMax: 60,
    panelMax: 40,
    rubric: [
      "1 poin: fakta jelas dan disertai jam (apa, klien mana, tenggat hari ini).",
      "1 poin: menyebut apa yang sudah dilakukan (tangkapan layar, mencoba ulang, komunikasi ke klien).",
      "1 poin: memisahkan yang sudah diketahui dari yang BELUM diketahui (tidak menyatakan bahwa tidak akan ada sanksi; tidak mengarang penyebab).",
      "1 poin: meminta sesuatu yang konkret dari senior (keputusan, kontak klien, instruksi) atau mengusulkan jam pengecekan berikutnya.",
    ],
    redFlags: [
      "menjanjikan tidak ada sanksi",
      "menyalahkan klien atau rekan",
      "catatan lebih dari 80 kata",
    ],
    reference:
      "Denda tetap SPT Masa PPN yang terlambat adalah Rp 500.000. Tidak ada bagian dari kunci yang bergantung pada aturan force majeure.",
  },
  m7: {
    panelMax: 150,
    criteria: [
      "Tenang dan mengakui kekhawatiran (3): mengakui kekhawatiran dalam satu kalimat, nada hangat dan profesional, tanpa mendramatisir dan tanpa meremehkan.",
      "Penjelasan jelas dan benar (3): menjelaskan dalam bahasa Inggris sederhana apa yang tampaknya terjadi (sanksi administrasi tetap Rp 100.000 karena keterlambatan SPT Tahunan, perlu diverifikasi), tanpa istilah berat dan tanpa mengarang fakta (tidak memastikan penyebab, tidak menyebut siapa yang salah).",
      "Langkah berikutnya yang konkret (3): meminta hal yang spesifik (foto yang jelas/PDF lengkap surat, konfirmasi tanggal) dan menyebut apa yang akan dilakukan tim; menyebut apa yang perlu dilakukan SEKARANG (mis. tidak perlu bertindak dalam beberapa jam ke depan, tetapi surat tidak boleh diabaikan).",
      'Komitmen dengan jam (3): memberi jam/hari balasan ("by 10:00 tomorrow" atau sejenisnya) dalam format Client Reply Library: "I will verify and come back to you by...". Tidak menjanjikan pembatalan sanksi, tidak menulis "I think it is fine".',
      'Bahasa Inggris dan keringkasan (3): bahasa Inggris benar dan natural, maksimal 150 kata, tidak menuduh, tidak ada "you should have...".',
    ],
    redFlags: [
      "menjanjikan pembatalan sanksi",
      'menuduh klien ("you submitted late")',
      "menyatakan tidak ada yang perlu dibayar tanpa verifikasi",
      "meminta data pribadi yang tidak perlu",
      "membalas dalam bahasa Indonesia/Italia",
      "tanpa tanda tangan/penutup",
    ],
    example:
      "Hi Tom, thank you for sending this right away, and I understand it looks worrying. From the photo, it appears to be a fixed administrative fine of Rp 100,000 related to your 2025 annual tax return. It is a small amount, and nothing is lost if we check it carefully first. Could you send a clear photo or PDF of the whole letter? I will check our filing records and come back to you by 10:00 tomorrow with what it is and what we recommend. Please do not worry about acting today. Kind regards, [name], Bali Zero",
  },
  m8: {
    best: { S1: "s1m", S2: "s2w", S3: "s3r", S4: "s4n" } as Record<
      string,
      string
    >,
    ok: { S1: "s1t", S2: "s2d", S3: "s3p", S4: "s4g" } as Record<
      string,
      string
    >,
    bad: { S1: "s1k", S2: "s2h", S3: "s3x", S4: "s4f" } as Record<
      string,
      string
    >,
    ptsBest: 25,
    ptsOk: 10,
    autoMax: 100,
    reasonMax: 125,
    panelMax: 50,
    rubric: [
      "1,25: alasan menyebut prinsip yang jelas (transparansi, rekening perusahaan, data yang benar, mengakui kesalahan, bertanya ke senior) secara spesifik.",
      '0,5: alasan umum ("karena itu benar") atau hanya mengulang pilihan.',
      "0: kosong, tidak konsisten dengan pilihan, atau membenarkan jalan pintas.",
    ],
    flagRule:
      'Jawaban "tidak dapat diterima" pada S1 atau S2, atau pada dua situasi mana pun oleh orang yang sama, wajib dibahas panel walaupun skor total tinggi.',
  },
  bands: [
    {
      min: 85,
      text: "85 sampai 100, kuat: hampir tanpa kesalahan angka dan jawaban manusia yang baik. Layak dilanjutkan dengan percaya diri.",
    },
    {
      min: 70,
      text: "70 sampai 84, cocok dengan pendampingan: tipikal junior yang cakap; lihat di mana kesalahan menumpuk (hitungan atau komunikasi).",
    },
    {
      min: 55,
      text: "55 sampai 69, batas: putuskan setelah wawancara; tes saja tidak cukup.",
    },
    { min: 0, text: "di bawah 55: belum cocok untuk peran ini saat ini." },
  ],
} as const;

/** Auto points of the whole test, in tenths: 60 (missions 1-5) + 6 (m6) + 10 (m8) = 76. */
export const AUTO_MAX_TENTHS = 760;
/** Panel-scored points, in tenths: m6 note 4, m7 15, m8 reasons 5. */
export const PANEL_MAX_TENTHS = 240;
