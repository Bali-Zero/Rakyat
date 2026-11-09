import "server-only";
import { emptyAnswers, type Answers, type Opt } from "../types";
import { CONTENT } from "./content";
import { AUTO_MAX_TENTHS, KEY, PANEL_MAX_TENTHS } from "./keys";

/** All points are integers in TENTHS; `fmt` renders them with a decimal comma. */
export interface Row {
  label: string;
  given: string;
  key: string;
  pts: number;
  max: number;
}

export interface MissionScore {
  id: string;
  no: number;
  title: string;
  auto: number;
  autoMax: number;
  panelMax: number;
  rows: Row[];
}

export interface Score {
  missions: MissionScore[];
  autoTotal: number;
  autoMax: number;
  panelMax: number;
  /** Situation ids answered with the unacceptable option. */
  unacceptable: string[];
}

export const fmt = (tenths: number): string =>
  (Math.round(tenths) / 10).toString().replace(".", ",");

const widgetOf = <K extends string>(id: string, kind: K) => {
  const m = CONTENT.missions.find((x) => x.id === id);
  if (!m || m.widget.kind !== kind)
    throw new Error(`content: ${id} is not ${kind}`);
  return m.widget as Extract<typeof m.widget, { kind: K }>;
};

const label = (opts: Opt[], id: string | undefined): string =>
  (id && opts.find((o) => o.id === id)?.label) || "(kosong)";

/** Rupiah from free text: dots and commas as thousands separators, or one decimal mark. */
export function parseRupiah(raw: string | undefined): number | null {
  if (!raw) return null;
  const t = raw.replace(/rp|idr/gi, "").replace(/\s+/g, "");
  let digits: string;
  if (/^\d{1,3}([.,]\d{3})+$/.test(t)) digits = t.replace(/[.,]/g, "");
  else if (
    /^\d{1,3}(\.\d{3})+,\d{1,2}$/.test(t) ||
    /^\d{1,3}(,\d{3})+\.\d{1,2}$/.test(t)
  )
    digits = t.replace(/[.,](?=\d{3}\b)/g, "").replace(",", ".");
  else if (/^\d+([.,]\d{1,2})?$/.test(t)) digits = t.replace(",", ".");
  else return null;
  const n = Number(digits);
  return Number.isFinite(n) ? n : null;
}

const str = (v: unknown, max: number): string =>
  typeof v === "string" ? v.slice(0, max) : "";
const rec = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
const ids = (v: unknown, valid: Set<string>): string[] =>
  Array.isArray(v)
    ? [
        ...new Set(
          v.filter((x): x is string => typeof x === "string" && valid.has(x)),
        ),
      ]
    : [];

/** Anything the browser sends is clipped and filtered to known ids: the keys never see raw input. */
export function normalizeAnswers(raw: unknown): Answers {
  const r = rec(raw);
  const out = emptyAnswers();

  const cards = new Set(KEY.m1.order);
  const order = Array.isArray(r.m1) ? ids(r.m1, cards) : [];
  out.m1 = order.length === cards.size ? order : null;

  for (const [k, v] of Object.entries(rec(r.m2)))
    if (k in KEY.m2.pairs) out.m2[k] = str(v, 20);

  const fields = new Set(
    widgetOf("m3_hotspots", "hotspot").doc.fields.map((f) => f.id),
  );
  out.m3 = ids(r.m3, fields);

  const m4ids = new Set<string>([
    ...KEY.m4.numeric.map((n) => n.id),
    KEY.m4.mcq.id,
  ]);
  for (const [k, v] of Object.entries(rec(r.m4)))
    if (m4ids.has(k)) out.m4[k] = str(v, 40);

  const m5ids = new Set<string>([...Object.keys(KEY.m5.rows), "q13", "q14"]);
  for (const [k, v] of Object.entries(rec(r.m5)))
    if (m5ids.has(k)) out.m5[k] = str(v, 30);

  const m6 = rec(r.m6);
  const portal = widgetOf("m6_portal_down", "portal");
  out.m6 = {
    checked: ids(m6.checked, new Set(portal.options.map((o) => o.id))),
    note: str(m6.note, 3000),
  };

  out.m7 = { reply: str(rec(r.m7).reply, 3000) };

  for (const s of widgetOf("m8_integrity", "integrity").situations) {
    const e = rec(rec(r.m8)[s.id]);
    const choice = str(e.choice, 10);
    out.m8[s.id] = {
      choice: s.options.some((o) => o.id === choice) ? choice : "",
      reason: str(e.reason, 600),
    };
  }
  return out;
}

const mission = (
  id: string,
  auto: number,
  autoMax: number,
  panelMax: number,
  rows: Row[],
): MissionScore => {
  const m = CONTENT.missions.find((x) => x.id === id)!;
  return { id, no: m.no, title: m.title, auto, autoMax, panelMax, rows };
};

function m1(a: Answers): MissionScore {
  const cards = widgetOf("m1_triage", "sort").cards;
  const rank = new Map<string, number>(KEY.m1.order.map((id, i) => [id, i]));
  let pairs = 0;
  if (a.m1) {
    for (let i = 0; i < 6; i++)
      for (let j = i + 1; j < 6; j++)
        if (rank.get(a.m1[i])! < rank.get(a.m1[j])!) pairs++;
  }
  const pts = Math.round((KEY.m1.max * pairs) / 15);
  return mission("m1_triage", pts, KEY.m1.max, 0, [
    {
      label: `Urutan kartu (${pairs}/15 pasangan benar)`,
      given: a.m1
        ? a.m1.map((id) => label(cards, id).slice(0, 38)).join(" > ")
        : "(tidak diubah)",
      key: KEY.m1.order.map((id) => label(cards, id).slice(0, 38)).join(" > "),
      pts,
      max: KEY.m1.max,
    },
  ]);
}

function m2(a: Answers): MissionScore {
  const parts = widgetOf("m2_calendar", "match").parts;
  const rows: Row[] = [];
  let right = 0;
  for (const p of parts)
    for (const r of p.rows) {
      const ok = a.m2[r.id] === KEY.m2.pairs[r.id];
      if (ok) right++;
      rows.push({
        label: `${r.id} ${r.label}`,
        given: label(p.options, a.m2[r.id]),
        key: label(p.options, KEY.m2.pairs[r.id]),
        pts: ok ? 1 : 0,
        max: 1,
      });
    }
  const total = Object.keys(KEY.m2.pairs).length;
  const pts = Math.round((KEY.m2.max * right) / total);
  rows.forEach(
    (r) => ((r.pts *= KEY.m2.max / total), (r.max *= KEY.m2.max / total)),
  );
  return mission("m2_calendar", pts, KEY.m2.max, 0, rows);
}

function m3(a: Answers): MissionScore {
  const fields = widgetOf("m3_hotspots", "hotspot").doc.fields;
  const rows: Row[] = [];
  let pts = 0;
  for (const f of fields) {
    const tapped = a.m3.includes(f.id);
    const wrong = (KEY.m3.wrong as readonly string[]).includes(f.id);
    if (!tapped && !wrong) continue;
    const p = tapped ? (wrong ? KEY.m3.perHit : KEY.m3.perFalseTap) : 0;
    pts += p;
    rows.push({
      label: `${f.label}: ${f.value}`,
      given: tapped ? "diketuk" : "tidak diketuk",
      key: wrong
        ? `SALAH. ${KEY.m3.reasons[f.id]}`
        : "benar (tidak perlu diketuk)",
      pts: p,
      max: wrong ? KEY.m3.perHit : 0,
    });
  }
  return mission("m3_hotspots", Math.max(0, pts), KEY.m3.max, 0, rows);
}

function m4(a: Answers): MissionScore {
  const rows: Row[] = KEY.m4.numeric.map((n) => {
    const got = parseRupiah(a.m4[n.id]);
    const ok = got !== null && Math.abs(got - n.value) <= KEY.m4.toleranceRp;
    return {
      label: n.label,
      given: a.m4[n.id]?.trim() || "(kosong)",
      key: `Rp ${n.value.toLocaleString("id-ID")}`,
      pts: ok ? n.pts : 0,
      max: n.pts,
    };
  });
  const mcq = widgetOf("m4_math", "math").problems[0].choice!;
  const mcqOk = a.m4[KEY.m4.mcq.id] === KEY.m4.mcq.right;
  rows.push({
    label: "Soal 1: apa yang salah di draf Mas Galih",
    given: label(mcq.options, a.m4[KEY.m4.mcq.id]),
    key: label(mcq.options, KEY.m4.mcq.right),
    pts: mcqOk ? KEY.m4.mcq.pts : 0,
    max: KEY.m4.mcq.pts,
  });
  return mission(
    "m4_math",
    rows.reduce((n, r) => n + r.pts, 0),
    KEY.m4.max,
    0,
    rows,
  );
}

function m5(a: Answers): MissionScore {
  const w = widgetOf("m5_recon", "recon");
  const rows: Row[] = w.rows.map((r) => {
    const ok = a.m5[r.id] === KEY.m5.rows[r.id];
    return {
      label: `${r.id} ${r.date} ${r.memo} (${r.dk} ${r.amount})`,
      given: label(w.statuses, a.m5[r.id]),
      key: `${label(w.statuses, KEY.m5.rows[r.id])}${KEY.m5.notes[r.id] ? `. ${KEY.m5.notes[r.id]}` : ""}`,
      pts: ok ? KEY.m5.ptsPerRow : 0,
      max: KEY.m5.ptsPerRow,
    };
  });
  for (const q of w.questions) {
    const right = q.id === "q13" ? KEY.m5.q13 : KEY.m5.q14;
    rows.push({
      label: q.text,
      given: label(q.options, a.m5[q.id]),
      key: label(q.options, right),
      pts: a.m5[q.id] === right ? KEY.m5.ptsPerQuestion : 0,
      max: KEY.m5.ptsPerQuestion,
    });
  }
  return mission(
    "m5_recon",
    rows.reduce((n, r) => n + r.pts, 0),
    KEY.m5.max,
    0,
    rows,
  );
}

function m6(a: Answers): MissionScore {
  const opts = widgetOf("m6_portal_down", "portal").options;
  let pts = 0;
  const rows: Row[] = [];
  for (const o of opts) {
    const right = (KEY.m6.right as readonly string[]).includes(o.id);
    const on = a.m6.checked.includes(o.id);
    const p = on ? (right ? KEY.m6.perRight : KEY.m6.perWrong) : 0;
    pts += p;
    rows.push({
      label: o.label,
      given: on ? "dicentang" : "tidak dicentang",
      key: right
        ? "BENAR (seharusnya dicentang)"
        : "SALAH (tidak boleh dicentang)",
      pts: p,
      max: right ? KEY.m6.perRight : 0,
    });
  }
  return mission(
    "m6_portal_down",
    Math.max(0, pts),
    KEY.m6.autoMax,
    KEY.m6.panelMax,
    rows,
  );
}

function m8(a: Answers): MissionScore & { bad: string[] } {
  const sits = widgetOf("m8_integrity", "integrity").situations;
  const rows: Row[] = [];
  const bad: string[] = [];
  let pts = 0;
  for (const s of sits) {
    const c = a.m8[s.id]?.choice ?? "";
    const p =
      c === KEY.m8.best[s.id]
        ? KEY.m8.ptsBest
        : c === KEY.m8.ok[s.id]
          ? KEY.m8.ptsOk
          : 0;
    if (c === KEY.m8.bad[s.id]) bad.push(s.id);
    pts += p;
    rows.push({
      label: `${s.id} ${s.title}`,
      given: label(s.options, c),
      key: `terbaik: ${label(s.options, KEY.m8.best[s.id])}`,
      pts: p,
      max: KEY.m8.ptsBest,
    });
  }
  return {
    ...mission("m8_integrity", pts, KEY.m8.autoMax, KEY.m8.panelMax, rows),
    bad,
  };
}

export function scoreAll(a: Answers): Score {
  const eight = m8(a);
  const { bad, ...m8score } = eight;
  const missions = [
    m1(a),
    m2(a),
    m3(a),
    m4(a),
    m5(a),
    m6(a),
    mission("m7_english_reply", 0, 0, KEY.m7.panelMax, []),
    m8score,
  ];
  return {
    missions,
    autoTotal: missions.reduce((n, m) => n + m.auto, 0),
    autoMax: AUTO_MAX_TENTHS,
    panelMax: PANEL_MAX_TENTHS,
    unacceptable: bad,
  };
}
