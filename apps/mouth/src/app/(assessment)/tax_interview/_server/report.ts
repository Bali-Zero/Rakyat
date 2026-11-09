import "server-only";
import {
  emptyField,
  flagsFor,
  type ExerciseTelemetry,
  type FieldTelemetry,
  type Flag,
} from "@/app/(assessment)/interview_natalie/telemetry";
import { MISSION_IDS, type Answers } from "../types";
import { CONTENT } from "./content";
import { KEY } from "./keys";
import { fmt, type Score } from "./score";

export const esc = (s: string): string =>
  s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const num = (v: unknown): number =>
  typeof v === "number" && Number.isFinite(v)
    ? Math.min(Math.max(v, 0), 1e10)
    : 0;

/** The browser's telemetry is untrusted input: rebuilt field by field, never spread. */
export function cleanTelemetry(
  raw: unknown,
): Record<string, ExerciseTelemetry> {
  const root =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const out: Record<string, ExerciseTelemetry> = {};
  for (const id of MISSION_IDS) {
    const e = (
      root[id] && typeof root[id] === "object" ? root[id] : {}
    ) as Record<string, unknown>;
    const fields: Record<string, FieldTelemetry> = {};
    const rf =
      e.fields && typeof e.fields === "object"
        ? (e.fields as Record<string, unknown>)
        : {};
    for (const [k, v] of Object.entries(rf).slice(0, 12)) {
      const f = (v && typeof v === "object" ? v : {}) as Record<
        string,
        unknown
      >;
      const base = emptyField();
      for (const key of Object.keys(base) as (keyof FieldTelemetry)[]) {
        if (key === "timeToFirstKeyMs")
          base[key] = typeof f[key] === "number" ? num(f[key]) : null;
        else base[key] = num(f[key]);
      }
      fields[k.slice(0, 12)] = base;
    }
    out[id] = {
      elapsedMs: num(e.elapsedMs),
      awayMs: num(e.awayMs),
      awayCount: num(e.awayCount),
      fields,
      autoLocked: false,
      restored: e.restored === true,
      composedChars: num(e.composedChars),
    };
  }
  return out;
}

const FLAG_ID: Record<string, string> = {
  KEYSTROKE_DEFICIT: "Karakter jauh lebih banyak daripada tombol yang ditekan",
  EXTERNAL_INSERT:
    "Teks masuk sekaligus dalam jumlah besar (seret-lepas, isi otomatis, dikte, atau jalur tempel lain)",
  PASTE_BLOCKED: "Percobaan menempel (paste) diblokir",
  COPY_OUT: "Menyalin atau memotong teks dari kolom jawaban",
  TAB_AWAY: "Meninggalkan tab",
  LOW_REVISION: "Hampir tanpa koreksi: teks ditulis tanpa revisi",
  SUSTAINED_SPEED: "Kecepatan mengetik tinggi dan konstan",
  RESTORED_SESSION:
    "Sesi dilanjutkan setelah muat ulang; hitungan ketikan tidak bermakna",
};

const mmss = (ms: number): string => {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

const wita = (ms: number): string =>
  `${new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Makassar",
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(ms))} WITA`;

const words = (t: string): number =>
  t.trim() ? t.trim().split(/\s+/).length : 0;

const TD =
  'style="border:1px solid #dad8d1;padding:6px 8px;vertical-align:top;font-size:13px"';
const TH =
  'style="border:1px solid #dad8d1;padding:6px 8px;background:#eae3d8;text-align:left;font-size:13px"';
const table = (head: string[], rows: string[][]): string =>
  `<table style="border-collapse:collapse;width:100%;margin:8px 0"><tr>${head
    .map((h) => `<th ${TH}>${esc(h)}</th>`)
    .join(
      "",
    )}</tr>${rows.map((r) => `<tr>${r.map((c) => `<td ${TD}>${c}</td>`).join("")}</tr>`).join("")}</table>`;
const box = (html: string): string =>
  `<div style="border:1px solid #a7a69f;background:#fffcf7;padding:10px 12px;margin:8px 0;white-space:pre-wrap;font-size:13px">${html}</div>`;
const ul = (items: readonly string[]): string =>
  `<ul style="margin:4px 0 8px 18px;padding:0;font-size:13px">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;

const widgetOf = (id: string) =>
  CONTENT.missions.find((m) => m.id === id)!.widget;

export interface ReportInput {
  name: string;
  attemptId: string;
  startedAt: number;
  endedAt: number;
  answers: Answers;
  telemetry: Record<string, ExerciseTelemetry>;
  score: Score;
}

export function buildReport(r: ReportInput): { subject: string; html: string } {
  const { score, answers: a, telemetry } = r;
  const flagsBy: Record<string, Flag[]> = {};
  for (const id of MISSION_IDS) flagsBy[id] = flagsFor(telemetry[id]);
  const allFlags = MISSION_IDS.flatMap((id) => flagsBy[id].map((f) => f.code));
  const codes = [...new Set(allFlags)].join(",") || "clean";
  const subject = `[Tax Interview] ${r.name.replace(/[\r\n]+/g, " ").slice(0, 80)} - skor otomatis ${fmt(score.autoTotal)}/${fmt(score.autoMax)} - ${codes}`;

  const hi = score.autoTotal;
  const lo = score.autoTotal + score.panelMax;
  const bandsInRange = KEY.bands.filter((b, i) => {
    const upper = i === 0 ? 1000 : KEY.bands[i - 1].min * 10;
    return upper > hi && b.min * 10 <= lo;
  });
  const integrityFlag =
    score.unacceptable.some((s) => s === "S1" || s === "S2") ||
    score.unacceptable.length >= 2;
  const m4 = score.missions.find((m) => m.id === "m4_math")!;

  let h = `<div style="font-family:Arial,Helvetica,sans-serif;color:#1d2c3b;max-width:880px">`;
  h += `<h2 style="margin:0 0 4px">Tes tertulis Junior Tax Consultant: Satu Hari di Meja Pajak</h2>`;
  h += `<p style="font-size:13px;margin:0 0 8px"><strong>Kandidat:</strong> ${esc(r.name)}<br/>`;
  h += `<strong>Mulai:</strong> ${wita(r.startedAt)}<br/><strong>Selesai:</strong> ${wita(r.endedAt)}<br/>`;
  h += `<strong>Durasi total:</strong> ${mmss(r.endedAt - r.startedAt)} (target 45 menit; tidak menggugurkan)<br/>`;
  h += `<strong>ID percobaan:</strong> ${esc(r.attemptId.slice(0, 8))}</p>`;
  h += box(
    `<strong>Skor otomatis: ${fmt(score.autoTotal)} dari ${fmt(score.autoMax)}.</strong>\nBagian yang dinilai manusia (${fmt(score.panelMax)} poin: catatan Misi 6, balasan Misi 7, alasan Misi 8) BELUM dinilai. Panel wajib menilainya sebelum ada skor akhir; skor di atas bukan skor akhir.`,
  );

  h += `<h3>Skor otomatis per misi</h3>`;
  h += table(
    ["Misi", "Waktu", "Otomatis", "Maks otomatis", "Perlu panel (maks)"],
    score.missions.map((m) => [
      esc(`${m.no}. ${m.title}`),
      mmss(telemetry[m.id].elapsedMs),
      m.autoMax ? fmt(m.auto) : "-",
      m.autoMax ? fmt(m.autoMax) : "-",
      m.panelMax ? fmt(m.panelMax) : "-",
    ]),
  );

  h += `<h3>Pembacaan skor (usulan, panel yang memutuskan)</h3>`;
  h += `<p style="font-size:13px">Total akhir berada antara ${fmt(hi)} (panel memberi 0) dan ${fmt(lo)} (panel memberi nilai penuh). Pita yang masih mungkin:</p>`;
  h += ul(bandsInRange.map((b) => b.text));
  h += `<p style="font-size:13px"><strong>Aturan yang berlaku terlepas dari pita:</strong></p>`;
  h += ul([
    integrityFlag
      ? `BENDERA INTEGRITAS: ada jawaban "tidak dapat diterima" (${score.unacceptable.join(", ") || "-"}). Wajib dibahas panel walaupun skor tinggi.`
      : "Bendera integritas: tidak ada jawaban yang tidak dapat diterima pada S1/S2 dan tidak ada dua jawaban semacam itu.",
    m4.auto < 70
      ? `Misi 4 di bawah 7/14 (${fmt(m4.auto)}/14): dasar hitungan lemah.`
      : `Misi 4 ${fmt(m4.auto)}/14: dasar hitungan memadai.`,
    "Misi 7 menunggu penilaian panel: di bawah 7/15 berarti komunikasi klien kurang, verifikasi saat wawancara.",
    "Baca bersama: waktu per misi (tidak menghukum, hanya informasi) dan pilihan Misi 6 (kebersihan bertindak di bawah tekanan).",
  ]);

  h += `<h3>Sinyal perilaku (bukan vonis)</h3>`;
  h += `<p style="font-size:13px">Setiap tanda menyatakan apa yang teramati, bukan artinya. Kesimpulan diambil panel dengan jawaban di depan mata.</p>`;
  h += table(
    ["Misi", "Waktu", "Meninggalkan tab", "Tanda"],
    score.missions.map((m) => {
      const t = telemetry[m.id];
      const f = flagsBy[m.id];
      return [
        esc(`${m.no}. ${m.title}`),
        mmss(t.elapsedMs),
        `${t.awayCount}x, ${mmss(t.awayMs)}`,
        f.length
          ? f
              .map(
                (x) =>
                  `<strong>${esc(x.code)}</strong> [${x.severity}]: ${esc(FLAG_ID[x.code] ?? x.code)}<br/><span style="color:#58626b">${esc(x.detail)}</span>`,
              )
              .join("<br/>")
          : "tidak ada",
      ];
    }),
  );

  h += `<h3>Jawaban otomatis dan kunci</h3>`;
  for (const m of score.missions.filter((x) => x.rows.length)) {
    h += `<h4 style="margin:12px 0 0">Misi ${m.no}. ${esc(m.title)}: ${fmt(m.auto)} / ${fmt(m.autoMax)}</h4>`;
    h += table(
      ["Butir", "Jawaban kandidat", "Kunci", "Poin"],
      m.rows.map((x) => [
        esc(x.label),
        esc(x.given),
        esc(x.key),
        `${fmt(x.pts)} / ${fmt(x.max)}`,
      ]),
    );
  }

  h += `<h3>Bagian untuk panel</h3>`;
  h += `<h4 style="margin:12px 0 0">Misi 6: catatan internal untuk Bu Lestari (0 sampai 4)</h4>`;
  h += box(a.m6.note.trim() ? esc(a.m6.note) : "(kosong)");
  h += `<p style="font-size:12px">${words(a.m6.note)} kata (batas 80). Nilai: ____ / 4</p>`;
  h += ul(KEY.m6.rubric);
  h += `<p style="font-size:13px"><strong>Tanda bahaya (catat, jangan hanya mengurangi poin):</strong></p>${ul(KEY.m6.redFlags)}`;
  h += `<p style="font-size:12px">Fakta acuan (hanya panel): ${esc(KEY.m6.reference)}</p>`;

  h += `<h4 style="margin:12px 0 0">Misi 7: balasan WhatsApp dalam bahasa Inggris (0 sampai 15)</h4>`;
  h += box(a.m7.reply.trim() ? esc(a.m7.reply) : "(kosong)");
  h += `<p style="font-size:12px">${words(a.m7.reply)} kata (batas 150). Nilai: ____ / 15</p>`;
  h += `<p style="font-size:13px">Lima kriteria, masing-masing 0 / 1 / 2 / 3:</p>${ul(KEY.m7.criteria)}`;
  h += `<p style="font-size:13px"><strong>Tanda bahaya (catat):</strong></p>${ul(KEY.m7.redFlags)}`;
  h += `<p style="font-size:13px">Contoh balasan yang baik (hanya panel):</p>${box(esc(KEY.m7.example))}`;

  h += `<h4 style="margin:12px 0 0">Misi 8: alasan tertulis (maks 5 poin; 0 / 0,5 / 1,25 per situasi)</h4>`;
  const sits = (
    widgetOf("m8_integrity") as Extract<
      ReturnType<typeof widgetOf>,
      { kind: "integrity" }
    >
  ).situations;
  h += table(
    ["Situasi", "Pilihan", "Alasan kandidat", "Nilai"],
    sits.map((s) => {
      const e = a.m8[s.id] ?? { choice: "", reason: "" };
      const tier =
        e.choice === KEY.m8.best[s.id]
          ? "terbaik"
          : e.choice === KEY.m8.ok[s.id]
            ? "dapat diterima"
            : e.choice
              ? "TIDAK DAPAT DITERIMA"
              : "(kosong)";
      return [
        esc(`${s.id} ${s.title}`),
        `${esc(s.options.find((o) => o.id === e.choice)?.label ?? "(kosong)")}<br/><em>${tier}</em>`,
        esc(e.reason.trim() || "(kosong)"),
        "____ / 1,25",
      ];
    }),
  );
  h += ul(KEY.m8.rubric);
  h += `<p style="font-size:13px">${esc(KEY.m8.flagRule)}</p>`;
  h += `<p style="font-size:12px;color:#58626b">Email otomatis dari halaman tes. Tidak ada data yang disimpan di server selain email ini.</p></div>`;

  return { subject, html: h };
}
