import { describe, expect, it, vi } from "vitest";

import { emptyAnswers, type Answers } from "../types";
import { KEY } from "../_server/keys";
import { buildReport, cleanTelemetry } from "../_server/report";
import { fmt, normalizeAnswers, parseRupiah, scoreAll } from "../_server/score";

/** Every auto answer right, built from the key itself: the "guilt" fixture. */
const perfect = (): Answers => ({
  m1: [...KEY.m1.order],
  m2: { ...KEY.m2.pairs },
  m3: [...KEY.m3.wrong],
  m4: {
    q41ppn: "1.650.000",
    q41total: "16650000",
    q41mcq: KEY.m4.mcq.right,
    q42: "Rp 21.800.000",
    q43w: "9,600,000",
    q43t: "86400000",
    q44: "6.750.000",
  },
  m5: { ...KEY.m5.rows, q13: KEY.m5.q13, q14: KEY.m5.q14 },
  m6: { checked: [...KEY.m6.right], note: "" },
  m7: { reply: "" },
  m8: Object.fromEntries(
    Object.entries(KEY.m8.best).map(([s, c]) => [s, { choice: c, reason: "" }]),
  ),
});

const by = (a: Answers) =>
  Object.fromEntries(
    scoreAll(normalizeAnswers(a)).missions.map((m) => [m.id, m.auto]),
  );

describe("scoring: all-correct and blank", () => {
  it("gives exactly 76 for every auto answer right, mission by mission", () => {
    const s = scoreAll(normalizeAnswers(perfect()));
    expect(s.autoTotal).toBe(760);
    expect(fmt(s.autoTotal)).toBe("76");
    expect(by(perfect())).toEqual({
      m1_triage: 100,
      m2_calendar: 100,
      m3_hotspots: 120,
      m4_math: 140,
      m5_recon: 140,
      m6_portal_down: 60,
      m7_english_reply: 0,
      m8_integrity: 100,
    });
    expect(s.autoTotal + s.panelMax).toBe(1000);
  });

  it("gives 0 for a blank submission and for garbage", () => {
    expect(scoreAll(normalizeAnswers(emptyAnswers())).autoTotal).toBe(0);
    expect(scoreAll(normalizeAnswers(null)).autoTotal).toBe(0);
    expect(
      scoreAll(normalizeAnswers({ m1: "x", m3: 5, m4: [], m8: "no" }))
        .autoTotal,
    ).toBe(0);
  });

  it("cannot be pushed above the maximum by padding with unknown ids", () => {
    const a = perfect();
    a.m3 = [...a.m3, "f1", "zzz", "f6"];
    a.m6.checked = [...a.m6.checked, "q1", "nope"];
    expect(scoreAll(normalizeAnswers(a)).autoTotal).toBeLessThanOrEqual(760);
  });
});

describe("m1 ranking pairs", () => {
  const key: string[] = [...KEY.m1.order];
  const concordant = (order: string[]) => {
    let n = 0;
    for (let i = 0; i < 6; i++)
      for (let j = i + 1; j < 6; j++)
        if (key.indexOf(order[i]) < key.indexOf(order[j])) n++;
    return n;
  };
  const perms: string[][] = [];
  const build = (rest: string[], acc: string[]) => {
    if (!rest.length) return void perms.push(acc);
    rest.forEach((x, i) =>
      build([...rest.slice(0, i), ...rest.slice(i + 1)], [...acc, x]),
    );
  };
  build([...KEY.m1.order], []);

  it("equals concordant pairs / 15 x 10 for all 720 orderings", () => {
    for (const p of perms) {
      const got = by({ ...emptyAnswers(), m1: p }).m1_triage;
      expect(got).toBe(Math.round((100 * concordant(p)) / 15));
    }
    expect(perms).toHaveLength(720);
  });

  it("spot checks: one adjacent swap loses exactly one pair, reversed is 0, untouched is 0", () => {
    const swap = [...KEY.m1.order];
    [swap[0], swap[1]] = [swap[1], swap[0]];
    expect(concordant(swap)).toBe(14);
    expect(by({ ...emptyAnswers(), m1: swap }).m1_triage).toBe(93);
    expect(
      by({ ...emptyAnswers(), m1: [...KEY.m1.order].reverse() }).m1_triage,
    ).toBe(0);
    expect(by({ ...emptyAnswers(), m1: null }).m1_triage).toBe(0);
  });

  it("treats a list with a duplicated or missing card as no answer", () => {
    const dup = [...KEY.m1.order];
    dup[5] = dup[0];
    expect(by({ ...emptyAnswers(), m1: dup }).m1_triage).toBe(0);
  });
});

describe("m2, m3, m6, m8", () => {
  it("m2: 9 right = 10, 4 right = 4.4", () => {
    const four = Object.fromEntries(Object.entries(KEY.m2.pairs).slice(0, 4));
    expect(by({ ...emptyAnswers(), m2: four }).m2_calendar).toBe(44);
    expect(
      by({ ...emptyAnswers(), m2: { ...KEY.m2.pairs, A1: "2026-12-10" } })
        .m2_calendar,
    ).toBe(89);
  });

  it("m3: +3 per wrong field, -1.5 per wrong tap, never below 0", () => {
    const m3 = (ids: string[]) =>
      by({ ...emptyAnswers(), m3: ids }).m3_hotspots;
    expect(m3(["f6"])).toBe(30);
    expect(m3([...KEY.m3.wrong])).toBe(120);
    expect(m3([...KEY.m3.wrong, "f1"])).toBe(105);
    expect(m3([...KEY.m3.wrong, "f1", "f2"])).toBe(90);
    expect(m3(["f1"])).toBe(0);
    expect(m3(["f1", "f2", "f3", "f4", "f5", "f6"])).toBe(0);
    expect(m3(["f6", "f1", "f2", "f3"])).toBe(0);
  });

  it("m6: +1.2 per right, -1.2 per wrong, floor 0, max 6", () => {
    const m6 = (checked: string[]) =>
      by({ ...emptyAnswers(), m6: { checked, note: "" } }).m6_portal_down;
    expect(m6([...KEY.m6.right])).toBe(60);
    expect(m6([...KEY.m6.right, "q6"])).toBe(48);
    expect(m6([...KEY.m6.right, ...KEY.m6.wrong])).toBe(24);
    expect(m6(["q6", "q2"])).toBe(0);
    expect(m6(["q1"])).toBe(12);
  });

  it("m8: best 2.5, acceptable 1, unacceptable 0; flags the unacceptable ones", () => {
    const pick = (tier: Record<string, string>) =>
      Object.fromEntries(
        Object.entries(tier).map(([s, c]) => [s, { choice: c, reason: "" }]),
      );
    expect(by({ ...emptyAnswers(), m8: pick(KEY.m8.ok) }).m8_integrity).toBe(
      40,
    );
    expect(by({ ...emptyAnswers(), m8: pick(KEY.m8.bad) }).m8_integrity).toBe(
      0,
    );
    const s = scoreAll(
      normalizeAnswers({ ...emptyAnswers(), m8: pick(KEY.m8.bad) }),
    );
    expect(s.unacceptable).toEqual(["S1", "S2", "S3", "S4"]);
  });
});

describe("m4 numeric: separators and tolerance", () => {
  it("parses dots, commas, Rp and spaces", () => {
    for (const t of [
      "1650000",
      "1.650.000",
      "1,650,000",
      "Rp 1.650.000",
      " rp1.650.000 ",
      "1.650.000,00",
      "1,650,000.00",
    ])
      expect(parseRupiah(t), t).toBe(1650000);
    for (const t of ["", "abc", "1.65.0", "-5", "12e3"])
      expect(parseRupiah(t), t).toBeNull();
  });

  it("accepts a difference of at most Rp 1, rejects Rp 2", () => {
    const q = (v: string) => by({ ...emptyAnswers(), m4: { q42: v } }).m4_math;
    expect(q("21800000")).toBe(40);
    expect(q("21800001")).toBe(40);
    expect(q("21799999")).toBe(40);
    expect(q("21800002")).toBe(0);
    expect(q("2180000")).toBe(0);
  });

  it("m4 points: 1.5 + 1.5 + 1 + 4 + 1.5 + 1.5 + 3 = 14, and the draft's 1.800.000 earns nothing", () => {
    expect(by(perfect()).m4_math).toBe(140);
    expect(
      by({
        ...emptyAnswers(),
        m4: { q41ppn: "1.800.000", q41total: "16.800.000", q41mcq: "p3" },
      }).m4_math,
    ).toBe(0);
  });
});

describe("the panel email", () => {
  const telemetry = cleanTelemetry({});
  const mk = (over: Partial<Answers> = {}, name = "Calon Uji") => {
    const answers = normalizeAnswers({ ...perfect(), ...over });
    return buildReport({
      name,
      attemptId: "11111111-1111-4111-8111-111111111111",
      startedAt: 1e12,
      endedAt: 1e12 + 2_400_000,
      answers,
      telemetry,
      score: scoreAll(answers),
    });
  };

  it("is in Bahasa Indonesia: rubrics are translated, none of the Italian survives", () => {
    const { html, subject } = mk();
    expect(subject).toContain("skor otomatis 76/76");
    expect(html).toContain("Skor otomatis: 76 dari 76");
    expect(html).toContain("Tenang dan mengakui kekhawatiran");
    expect(html).toContain("Komitmen dengan jam");
    expect(html).toContain("BELUM dinilai");
    expect(html).not.toMatch(/Calma|riconoscimento|Segnali|punteggio|criteri/i);
  });

  it("escapes candidate text and flags unacceptable integrity answers", () => {
    const { html } = mk(
      {
        m7: { reply: "<script>alert(1)</script>" },
        m8: { S1: { choice: "s1k", reason: "<b>x</b>" } },
      },
      "<img src=x onerror=1>",
    );
    expect(html).not.toContain("<script>alert");
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("BENDERA INTEGRITAS");
  });

  it("cleanTelemetry rebuilds untrusted telemetry without spreading it", () => {
    const t = cleanTelemetry({
      m7_english_reply: {
        elapsedMs: "9",
        awayMs: -4,
        evil: 1,
        fields: { f: { chars: 5, polluted: 9 } },
      },
    });
    expect(t.m7_english_reply.elapsedMs).toBe(0);
    expect(t.m7_english_reply.awayMs).toBe(0);
    expect(t.m7_english_reply.fields.f.chars).toBe(5);
    expect(t.m7_english_reply).not.toHaveProperty("evil");
    expect(t.m7_english_reply.fields.f).not.toHaveProperty("polluted");
  });
});
