import "server-only";
import type { Content } from "../types";

/**
 * Candidate-facing story. All data is fictional: names, NPWP, companies, sums.
 *
 * Option ids are OPAQUE on purpose (c1, q4, s1k ...): the key lives in
 * `./keys` and an id that read "o6 = wrong" or "a = best" would leak it through
 * the page props. Initial orders are scrambled for the same reason.
 */
export const CONTENT: Content = {
  title: "Satu Hari di Meja Pajak",
  subtitle:
    "Hari pertamamu di Bali Zero, Senin 30 November 2026, 08.30 sampai 17.00.",
  personas: {
    lestari: { name: "Bu Lestari", role: "Kepala tim pajak", initials: "BL" },
    galih: { name: "Mas Galih", role: "Konsultan senior", initials: "MG" },
    nindy: { name: "Kak Nindy", role: "Admin kantor", initials: "KN" },
    naomi: {
      name: "Bu Naomi",
      role: "Direktur PT Laut Biru Fiktif",
      initials: "BN",
    },
    hartley: { name: "Mr. Tom Hartley", role: "Klien (expat)", initials: "TH" },
  },
  opening: {
    from: "lestari",
    at: "08.28",
    text: "Selamat datang! Hari ini Senin 30 November, tanggal PPN kita jatuh tempo, jadi hari ini ramai. Jangan khawatir, kamu tidak sendirian. Buka chat kamu dan kita mulai.",
  },
  closing: {
    from: "lestari",
    at: "17.00",
    text: "Terima kasih untuk hari ini. Besok kita bicarakan hasilnya bersama.",
  },
  missions: [
    {
      id: "m1_triage",
      no: 1,
      storyTime: "08.35",
      minutes: 4,
      title: "Inbox pagi",
      messages: [
        {
          from: "lestari",
          at: "08.35",
          text: "Pagi. Ini enam hal yang masuk sejak Jumat. Hari ini Senin 30 November. Urutkan dari yang paling mendesak ke yang paling santai. Seret kartunya.",
        },
      ],
      widget: {
        kind: "sort",
        cards: [
          {
            id: "c2",
            label:
              "Pengingat sistem: lapor SPT Masa PPh Unifikasi Masa November, batas 20 Desember.",
          },
          {
            id: "c1",
            label:
              "Mr. Hartley mengirim foto sebuah surat dari kantor pajak. Di surat tertulis tanggapan paling lambat 4 Desember.",
          },
          {
            id: "c4",
            label:
              "Mas Galih: tolong rapikan template checklist internal kalau sempat, tidak ada tenggat.",
          },
          {
            id: "c3",
            label:
              "Kak Nindy: Bu Naomi (PT Laut Biru Fiktif) belum kirim konfirmasi untuk SPT Masa PPN Oktober. Batas lapor dan setor PPN: 30 November (hari ini).",
          },
          {
            id: "c6",
            label:
              "Pengingat sistem: setor PPh Pasal 25 Masa November, batas 15 Desember.",
          },
          {
            id: "c5",
            label:
              "Pak Dimas: Faktur pajak bulan lalu salah nama pembeli. Ia butuh yang sudah diperbaiki besok pagi jam 09.00 (1 Desember) karena akan bayar.",
          },
        ],
      },
    },
    {
      id: "m2_calendar",
      no: 2,
      storyTime: "09.20",
      minutes: 4,
      title: "Kalender kewajiban",
      messages: [
        {
          from: "nindy",
          at: "09.20",
          text: "Aku lagi isi papan kalender tim, tapi tanggalnya terlepas dari kartunya, hehe. Bantu pasangkan ya. Dua tanggal tidak dipakai. Lalu pasangkan juga denda tetap keterlambatan.",
        },
      ],
      widget: {
        kind: "match",
        parts: [
          {
            title: "Kewajiban ke tanggal",
            note: "Pakai batas normal menurut aturan; abaikan hari libur dan akhir pekan.",
            rows: [
              { id: "A3", label: "Setor dan lapor SPT Masa PPN Masa November" },
              {
                id: "A5",
                label:
                  "SPT Tahunan PPh Badan Tahun Pajak 2026 (tahun buku kalender)",
              },
              { id: "A1", label: "Setor PPh Pasal 25 Masa November" },
              {
                id: "A4",
                label: "SPT Tahunan PPh Orang Pribadi Tahun Pajak 2026",
              },
              {
                id: "A2",
                label: "Lapor SPT Masa PPh Unifikasi Masa November",
              },
            ],
            options: [
              { id: "2026-12-10", label: "10 Desember 2026" },
              { id: "2026-12-15", label: "15 Desember 2026" },
              { id: "2026-12-20", label: "20 Desember 2026" },
              { id: "2026-12-31", label: "31 Desember 2026" },
              { id: "2027-01-31", label: "31 Januari 2027" },
              { id: "2027-03-31", label: "31 Maret 2027" },
              { id: "2027-04-30", label: "30 April 2027" },
            ],
          },
          {
            title: "Denda tetap keterlambatan",
            note: "Satu nominal bisa dipakai lebih dari sekali.",
            rows: [
              { id: "B3", label: "Terlambat SPT Tahunan PPh Badan" },
              { id: "B1", label: "Terlambat SPT Masa PPN" },
              { id: "B4", label: "Terlambat SPT Tahunan PPh Orang Pribadi" },
              { id: "B2", label: "Terlambat SPT Masa PPh (mis. Unifikasi)" },
            ],
            options: [
              { id: "50000", label: "Rp 50.000" },
              { id: "100000", label: "Rp 100.000" },
              { id: "200000", label: "Rp 200.000" },
              { id: "500000", label: "Rp 500.000" },
              { id: "1000000", label: "Rp 1.000.000" },
            ],
          },
        ],
      },
    },
    {
      id: "m3_hotspots",
      no: 3,
      storyTime: "10.10",
      minutes: 4,
      title: "Cari kesalahannya",
      messages: [
        {
          from: "galih",
          at: "10.10",
          text: "Aku mau kirim bukti potong ini ke klien sebelum makan siang, tapi mataku sudah lelah. Tolong lihat sekilas, ya. Ketuk kolom yang menurutmu salah. Invoice dari penerima ada di sebelah kanan sebagai pembanding.",
        },
      ],
      widget: {
        kind: "hotspot",
        invoice: {
          title: "Invoice dari CV Cahaya Contoh Fiktif",
          lines: [
            { label: "Penjual", value: "CV Cahaya Contoh Fiktif" },
            { label: "NPWP penjual", value: "0000 0000 0000 0002" },
            { label: "Pembeli", value: "PT Karang Fiktif Sejahtera" },
            { label: "Jasa konsultansi", value: "Rp 20.000.000" },
            { label: "PPN efektif 11%", value: "Rp 2.200.000" },
            { label: "Total", value: "Rp 22.200.000" },
            { label: "Tanggal bayar", value: "27 November 2026" },
          ],
        },
        doc: {
          title: "Bukti Pemotongan PPh Pasal 23 (CONTOH FIKTIF)",
          fields: [
            { id: "f1", label: "Nomor", value: "BPPU-FIKTIF-0001" },
            { id: "f2", label: "Masa Pajak", value: "November 2026" },
            {
              id: "f3",
              label: "Pemotong (nama)",
              value: "PT Karang Fiktif Sejahtera",
            },
            {
              id: "f4",
              label: "NPWP Pemotong",
              value: "0000 0000 0000 0001",
            },
            {
              id: "f5",
              label: "Penerima Penghasilan (nama)",
              value: "CV Cahaya Contoh Fiktif",
            },
            {
              id: "f6",
              label: "NPWP Penerima",
              value: "0000 0000 0000 0020",
            },
            { id: "f7", label: "Jenis Penghasilan", value: "Jasa konsultansi" },
            {
              id: "f8",
              label: "Dasar Pengenaan Pajak (DPP)",
              value: "Rp 22.200.000",
            },
            { id: "f9", label: "Tarif", value: "15%" },
            { id: "f10", label: "PPh Dipotong", value: "Rp 3.330.000" },
            {
              id: "f11",
              label: "Tanggal Pemotongan",
              value: "27 November 2026",
            },
            {
              id: "f12",
              label: "Penandatangan",
              value: "Direktur PT Karang Fiktif Sejahtera",
            },
          ],
        },
      },
    },
    {
      id: "m4_math",
      no: 4,
      storyTime: "11.00",
      minutes: 6,
      title: "Hitung cepat",
      messages: [
        {
          from: "galih",
          at: "11.00",
          text: "Maaf ya, aku lagi terburu-buru. Ini draf invoice untuk klien kita dari hitunganku, tolong cek cepat: jasa Rp 15.000.000, PPN aku tulis Rp 1.800.000, total Rp 16.800.000. Sudah aman kan?",
        },
      ],
      widget: {
        kind: "math",
        problems: [
          {
            id: "q41",
            title: "Soal 1",
            text: "Hitung PPN yang benar untuk jasa Rp 15.000.000 (PPN dihitung dengan tarif efektif seperti aturan saat ini). Berapa PPN yang benar dan berapa total invoice yang benar?",
            inputs: [
              { id: "q41ppn", label: "PPN yang benar (Rp)" },
              { id: "q41total", label: "Total invoice yang benar (Rp)" },
            ],
            choice: {
              id: "q41mcq",
              text: "Menurutmu, apa yang salah di draf Mas Galih?",
              options: [
                { id: "p2", label: "Ia lupa menjumlahkan" },
                { id: "p3", label: "Tidak ada yang salah" },
                {
                  id: "p1",
                  label: "Ia memakai 12% × seluruh Rp 15.000.000",
                },
              ],
            },
          },
          {
            id: "q42",
            title: "Soal 2",
            text: "Klien PT Karang Fiktif Sejahtera membayar jasa konsultansi: nilai jasa Rp 20.000.000, PPN efektif 11% dibayar penuh ke penyedia, dan klien memotong PPh Pasal 23 sebesar 2% dari nilai jasa. Berapa uang yang ditransfer klien ke penyedia?",
            inputs: [{ id: "q42", label: "Ditransfer ke penyedia (Rp)" }],
          },
          {
            id: "q43",
            title: "Soal 3",
            text: "PT PMA kita menyewa kantor dari pemilik perorangan: sewa setahun Rp 96.000.000 dibayar di muka. PT PMA wajib memotong PPh Final Pasal 4 ayat (2) 10% dari nilai sewa. Berapa yang dipotong, dan berapa yang ditransfer ke pemilik?",
            inputs: [
              { id: "q43w", label: "Dipotong (Rp)" },
              { id: "q43t", label: "Ditransfer ke pemilik (Rp)" },
            ],
          },
          {
            id: "q44",
            title: "Soal 4",
            text: "Karyawan fiktif, status TK/0 (PTKP Rp 54.000.000 setahun). Penghasilan neto setahun Rp 139.000.000. Hitung PPh 21 terutang setahun memakai tarif progresif Pasal 17: 5% sampai Rp 60 juta, 15% untuk bagian berikutnya sampai Rp 250 juta.",
            inputs: [{ id: "q44", label: "PPh 21 terutang setahun (Rp)" }],
          },
        ],
      },
    },
    {
      id: "m5_recon",
      no: 5,
      storyTime: "13.00",
      minutes: 8,
      title: "Mutasi rekening vs invoice",
      messages: [
        {
          from: "nindy",
          at: "13.00",
          text: "Setelah makan siang ada kerjaan yang paling aku suka: mencocokkan. Rekening koran Oktober PT Kopi Senja Fiktif sudah masuk, ini daftar invoice mereka. Tolong tandai statusnya untuk tiap mutasi. Kolom 'Dicatat sebagai' adalah yang sudah dicatat di buku.",
        },
      ],
      widget: {
        kind: "recon",
        invoices: [
          ["No", "Pelanggan", "DPP", "PPN 11%", "Total"],
          [
            "INV-101",
            "Kafe Tepi Pantai",
            "10.000.000",
            "1.100.000",
            "11.100.000",
          ],
          [
            "INV-102",
            "Hotel Bukit Fiktif",
            "20.000.000",
            "2.200.000",
            "22.200.000",
          ],
          ["INV-103", "Toko Sari Rasa", "5.000.000", "550.000", "5.550.000"],
          ["INV-104", "Warung Nusa", "3.000.000", "330.000", "3.330.000"],
          [
            "INV-105",
            "Resto Lembayung",
            "15.000.000",
            "1.650.000",
            "16.650.000",
          ],
          ["INV-106", "Kafe Awan", "7.500.000", "825.000", "8.325.000"],
          [
            "INV-107",
            "Villa Tiga Pagi",
            "30.000.000",
            "3.300.000",
            "33.300.000",
          ],
          [
            "INV-108",
            "Kafe Kintamani Contoh",
            "6.000.000",
            "660.000",
            "6.660.000",
          ],
        ],
        statuses: [
          { id: "cocok", label: "Cocok dengan invoice" },
          { id: "selisih", label: "Selisih jumlah" },
          { id: "ganda", label: "Pembayaran ganda" },
          { id: "salah_klasifikasi", label: "Salah klasifikasi" },
          { id: "dokumen", label: "Perlu dokumen pendukung" },
          { id: "normal", label: "Normal, tidak ada masalah" },
        ],
        rows: [
          {
            id: "r1",
            date: "02/10",
            memo: "TRF KAFE TEPI PANTAI INV-101",
            dk: "K",
            amount: "11.100.000",
            booked: "Penjualan",
          },
          {
            id: "r2",
            date: "05/10",
            memo: "TRF HOTEL BUKIT FIKTIF INV 102",
            dk: "K",
            amount: "22.200.000",
            booked: "Penjualan",
          },
          {
            id: "r3",
            date: "06/10",
            memo: "TRF TOKO SARI RASA INV-103",
            dk: "K",
            amount: "5.500.000",
            booked: "Penjualan",
          },
          {
            id: "r4",
            date: "08/10",
            memo: "TRF WARUNG NUSA INV-104",
            dk: "K",
            amount: "3.330.000",
            booked: "Penjualan",
          },
          {
            id: "r5",
            date: "08/10",
            memo: "BIAYA ADM BANK",
            dk: "D",
            amount: "150.000",
            booked: "Biaya bank",
          },
          {
            id: "r6",
            date: "09/10",
            memo: "TRF RESTO LEMBAYUNG INV-105",
            dk: "K",
            amount: "16.650.000",
            booked: "Penjualan",
          },
          {
            id: "r7",
            date: "12/10",
            memo: "TRF KAFE AWAN INV-106",
            dk: "K",
            amount: "8.325.000",
            booked: "Penjualan",
          },
          {
            id: "r8",
            date: "12/10",
            memo: "TRF KAFE AWAN INV-106",
            dk: "K",
            amount: "8.325.000",
            booked: "Penjualan",
          },
          {
            id: "r9",
            date: "15/10",
            memo: "SETOR PPN MASA 09 KODE BILLING",
            dk: "D",
            amount: "4.000.000",
            booked: "Biaya lain-lain",
          },
          {
            id: "r10",
            date: "18/10",
            memo: "TRF VILLA TIGA PAGI INV-107",
            dk: "K",
            amount: "32.700.000",
            booked: "Penjualan",
          },
          {
            id: "r11",
            date: "20/10",
            memo: "SETORAN TUNAI",
            dk: "K",
            amount: "10.000.000",
            booked: "Penjualan",
          },
          {
            id: "r12",
            date: "25/10",
            memo: "TRF GAJI OKT",
            dk: "D",
            amount: "12.000.000",
            booked: "Gaji",
          },
        ],
        questions: [
          {
            id: "q13",
            text: "Invoice mana yang belum ada pembayarannya sama sekali di bulan Oktober?",
            options: [
              "INV-103",
              "INV-104",
              "INV-105",
              "INV-107",
              "INV-108",
            ].map((v) => ({ id: v, label: v })),
          },
          {
            id: "q14",
            text: "Selisih Rp 600.000 di r10 paling mungkin karena apa?",
            options: [
              { id: "t2", label: "Biaya transfer bank" },
              { id: "t3", label: "Diskon yang disepakati" },
              {
                id: "t1",
                label:
                  "Pelanggan memotong PPh Pasal 23 sebesar 2% dari Rp 30.000.000",
              },
              { id: "t4", label: "Kesalahan ketik bank" },
            ],
          },
        ],
      },
    },
    {
      id: "m6_portal_down",
      no: 6,
      storyTime: "15.40",
      minutes: 6,
      title: "Portal macet, klien cemas",
      messages: [
        {
          from: "naomi",
          at: "15.40",
          text: "Sore. Coretax error terus dari tadi: 'layanan tidak tersedia'. PPN kami jatuh tempo HARI INI. Kalau telat nanti kena denda, kan? Tolong, saya takut sekali.",
        },
        {
          from: "nindy",
          at: "15.40",
          text: "Aku tempelkan tangkapan layar errornya ya. Bu Lestari sedang rapat di luar.",
          attachment: {
            variant: "error",
            title: "Layanan tidak tersedia",
            lines: [
              "Coretax sedang tidak dapat diakses.",
              "Silakan coba beberapa saat lagi.",
            ],
            foot: "Tangkapan layar contoh, jam 15.38",
          },
        },
      ],
      widget: {
        kind: "portal",
        pick: "Pilih semua langkah yang kamu lakukan sekarang.",
        options: [
          {
            id: "q1",
            label:
              "Beri tahu Bu Naomi sekarang secara jujur: kami sedang mencoba, kondisi sistem belum bisa dipastikan, dan kami kabari lagi jam tertentu",
          },
          {
            id: "q2",
            label:
              "Minta Bu Naomi mengirim password Coretax agar kamu coba dari perangkat lain",
          },
          {
            id: "q3",
            label:
              "Beri tahu Bu Lestari dan Mas Galih agar mereka tahu kondisinya",
          },
          {
            id: "q4",
            label:
              "Simpan tangkapan layar error lengkap dengan jam sebagai bukti",
          },
          { id: "q5", label: "Tunggu sampai besok, jangan bilang siapa-siapa" },
          {
            id: "q6",
            label:
              'Katakan ke Bu Naomi, "Tenang, tidak akan kena denda karena sistemnya rusak"',
          },
          {
            id: "q7",
            label:
              "Coba lagi secara berkala dan cek pengumuman resmi DJP tentang gangguan",
          },
          {
            id: "q8",
            label:
              "Siapkan dulu data dan dokumen PPN supaya begitu portal pulih bisa langsung dikirim",
          },
        ],
        prompt:
          "Tulis catatan internal singkat untuk Bu Lestari (maksimal 80 kata, bahasa Indonesia): apa yang terjadi, apa yang sudah kamu lakukan, apa yang belum diketahui, dan apa yang kamu butuhkan darinya.",
        maxWords: 80,
      },
    },
    {
      id: "m7_english_reply",
      no: 7,
      storyTime: "16.30",
      minutes: 7,
      title: "WhatsApp dari klien cemas",
      messages: [
        {
          from: "hartley",
          at: "16.28",
          en: true,
          text: "Hi, I just got this letter from the tax office in the post. It says something about a fine?? Do I have to pay today? I'm leaving for Singapore tomorrow morning. Did you guys mess up my tax return?? I'm really worried.",
          attachment: {
            variant: "letter",
            title: "SURAT TAGIHAN PAJAK (STP)",
            lines: [
              "Wajib Pajak: T. H. (fiktif)",
              "Jenis: sanksi administrasi denda, Pasal 7 ayat (1) UU KUP",
              "Tahun Pajak 2025",
              "Jumlah: Rp 100.000",
              "Tanggapan/pembayaran paling lambat: 4 Desember 2026",
            ],
            foot: "Foto surat (contoh fiktif)",
          },
        },
      ],
      widget: {
        kind: "reply",
        infoTitle: "Info dari Bu Lestari",
        info: "Klien ini sudah klien kami; SPT Tahunan 2025-nya kami kirim. Tanggal pengiriman akan kami cek di arsip. Jangan menjawab dari ingatan; jangan janjikan surat ini bisa dibatalkan.",
        maxWords: 150,
      },
    },
    {
      id: "m8_integrity",
      no: 8,
      storyTime: "16.50",
      minutes: 6,
      title: "Zona grigia",
      messages: [
        {
          from: "lestari",
          at: "16.50",
          text: "Terakhir. Ini bukan soal angka. Ini soal cara kamu mengambil keputusan kalau tidak ada yang melihat. Jawab apa adanya.",
        },
      ],
      widget: {
        kind: "integrity",
        maxWords: 25,
        situations: [
          {
            id: "S1",
            title: "Pembayaran ke rekening pribadi",
            text: "Seorang klien menawarkan potongan harga 10% kalau biaya jasa dibayar ke rekening pribadi seorang staf, bukan ke rekening perusahaan.",
            options: [
              {
                id: "s1k",
                label: "Terima, karena klien senang dan uangnya tetap masuk.",
              },
              {
                id: "s1m",
                label:
                  "Tolak dengan sopan; pembayaran hanya ke rekening perusahaan dengan invoice; laporkan ke Bu Lestari.",
              },
              { id: "s1t", label: "Tolak tapi tidak memberi tahu siapa pun." },
            ],
          },
          {
            id: "S2",
            title: "Menurunkan angka lapor",
            text: "Klien meminta omzet bulan ini ditulis lebih kecil di SPT Masa 'supaya pajaknya lebih ringan, angka aslinya buat kita saja'.",
            options: [
              {
                id: "s2d",
                label: 'Bilang "saya tanya senior dulu" tanpa menolak.',
              },
              {
                id: "s2h",
                label: "Turunkan sedikit saja supaya tidak terlalu kelihatan.",
              },
              {
                id: "s2w",
                label:
                  "Jelaskan bahwa kami hanya melaporkan angka yang sebenarnya dan sesuai dokumen; teruskan permintaan ke Bu Lestari.",
              },
            ],
          },
          {
            id: "S3",
            title: "Tanggal mundur",
            text: "Seorang rekan meminta kamu menerbitkan dokumen dengan tanggal mundur (backdate) supaya tampak tepat waktu.",
            options: [
              { id: "s3p", label: "Tolak dan diam saja." },
              {
                id: "s3r",
                label:
                  "Tolak membuat dokumen bertanggal mundur; sampaikan ke Bu Lestari.",
              },
              { id: "s3x", label: "Lakukan, tetapi hanya sekali ini." },
            ],
          },
          {
            id: "S4",
            title: "Kesalahan sendiri",
            text: "Seminggu lalu kamu mengirim dokumen dengan satu angka yang salah. Belum ada yang sadar.",
            options: [
              {
                id: "s4f",
                label: "Biarkan saja, mungkin tidak ada yang melihat.",
              },
              { id: "s4g", label: "Perbaiki diam-diam tanpa memberi tahu." },
              {
                id: "s4n",
                label: "Beri tahu Bu Lestari sekarang dan perbaiki bersama.",
              },
            ],
          },
        ],
      },
    },
  ],
};
