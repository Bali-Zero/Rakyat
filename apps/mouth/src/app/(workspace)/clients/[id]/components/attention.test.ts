import { describe, expect, it } from "vitest";
import type { AttentionItem } from "@/lib/api/crm/crm.types";
import {
  attentionText,
  buildLedger,
  attentionTone,
  shortDate,
  sortAttention,
  stateWord,
} from "./attention";

// 3 Nov 2026, noon WITA.
const NOW = Date.parse("2026-11-03T04:00:00Z");

const item = (over: Partial<AttentionItem>): AttentionItem => ({
  kind: "permit",
  subject: "client",
  subject_id: 1,
  subject_name: null,
  label: "KITAS",
  code: null,
  state: "urgent",
  date: null,
  opens_on: null,
  days: null,
  note: null,
  document_id: null,
  ...over,
});

describe("attentionText — every kind x state", () => {
  const permit = {
    kind: "permit" as const,
    label: "Investor KITAS",
    code: "E28A",
  };
  const passport = { kind: "passport" as const, label: "Passport" };
  const lkpm = { kind: "lkpm" as const, label: "LKPM Q4 2026" };
  const sptP = { kind: "spt_personal" as const, label: "SPT personal 2025" };
  const sptC = { kind: "spt_company" as const, label: "SPT company 2025" };

  const table: [string, Partial<AttentionItem>, string][] = [
    [
      "permit upcoming",
      { ...permit, state: "upcoming", days: 62, date: "2027-01-04" },
      "E28A · Investor KITAS expires 4 Jan 2027 · 62 days",
    ],
    [
      "permit urgent",
      { ...permit, state: "urgent", days: 5, date: "2026-11-08" },
      "E28A · Investor KITAS expires 8 Nov 2026 · 5 days",
    ],
    [
      "permit expired",
      { ...permit, state: "expired", days: -3, date: "2026-10-31" },
      "E28A · Investor KITAS expired 31 Oct 2026 · 3 days ago",
    ],
    [
      "permit, label already led by its code",
      {
        ...permit,
        label: "E28A — Investor KITAS (Standard)",
        state: "urgent",
        days: 1,
        date: "2026-11-04",
      },
      "E28A · Investor KITAS expires 4 Nov 2026 · 1 day",
    ],
    [
      "permit itk missing",
      {
        ...permit,
        code: null,
        label: "Visit visa",
        state: "urgent",
        days: 11,
        date: "2026-11-14",
        note: "itk_missing",
      },
      "Visit visa expires 14 Nov 2026 · 11 days · ITK missing",
    ],
    [
      "passport upcoming",
      { ...passport, state: "upcoming", days: 60, date: "2027-01-02" },
      "Passport expires 2 Jan 2027 · 60 days",
    ],
    [
      "passport expired",
      { ...passport, state: "expired", days: -9, date: "2026-10-25" },
      "Passport expired 25 Oct 2026 · 9 days ago",
    ],
    [
      "family passport",
      {
        ...passport,
        subject: "family_member",
        subject_name: "Sample Child",
        state: "upcoming",
        days: 60,
        date: "2027-01-02",
      },
      "Sample Child · Passport expires 2 Jan 2027 · 60 days",
    ],
    [
      "family permit",
      {
        ...permit,
        code: "E31A",
        label: "Spouse KITAS",
        subject: "family_member",
        subject_name: "Sample Spouse",
        state: "urgent",
        days: 20,
        date: "2026-11-23",
      },
      "Sample Spouse · E31A · Spouse KITAS expires 23 Nov 2026 · 20 days",
    ],
    [
      "lkpm upcoming",
      { ...lkpm, state: "upcoming", days: 6, opens_on: "2026-11-09" },
      "LKPM Q4 2026 opens 9 Nov 2026 · in 6 days",
    ],
    [
      "lkpm open",
      { ...lkpm, state: "open", days: 73, date: "2027-01-15" },
      "LKPM Q4 2026 window closes 15 Jan 2027",
    ],
    ["lkpm done", { ...lkpm, state: "done" }, "LKPM Q4 2026 filed"],
    [
      "lkpm overdue",
      { ...lkpm, state: "overdue", date: "2026-10-15" },
      "LKPM Q4 2026 overdue since 15 Oct 2026",
    ],
    [
      "lkpm company name follows",
      {
        ...lkpm,
        subject: "company",
        subject_name: "PT Contoh Abadi",
        state: "done",
      },
      "LKPM Q4 2026 filed · PT Contoh Abadi",
    ],
    [
      "spt personal upcoming",
      { ...sptP, state: "upcoming", days: 40, date: "2026-12-13" },
      "SPT personal 2025 due 13 Dec 2026 · 40 days",
    ],
    [
      "spt personal urgent",
      { ...sptP, state: "urgent", days: 3, date: "2026-11-06" },
      "SPT personal 2025 due 6 Nov 2026 · 3 days",
    ],
    [
      "spt personal done",
      { ...sptP, state: "done" },
      "SPT personal 2025 filed",
    ],
    [
      "spt company overdue",
      { ...sptC, state: "overdue", date: "2026-04-30" },
      "SPT company 2025 overdue since 30 Apr 2026",
    ],
    [
      "spt company upcoming",
      { ...sptC, state: "upcoming", days: 150, date: "2027-03-31" },
      "SPT company 2025 due 31 Mar 2027 · 150 days",
    ],
  ];

  it.each(table)("%s", (_name, over, expected) => {
    expect(attentionText(item(over), NOW)).toBe(expected);
  });

  it("GUILT: a permit or passport never reads 'opens' (that is LKPM wording)", () => {
    for (const state of ["upcoming", "urgent", "open"] as const) {
      expect(
        attentionText(
          item({ ...permit, state, days: 30, date: "2026-12-03" }),
          NOW,
        ),
      ).not.toMatch(/opens/);
      expect(
        attentionText(
          item({ ...passport, state, days: 30, date: "2026-12-03" }),
          NOW,
        ),
      ).not.toMatch(/opens/);
    }
  });
});

describe("tone, word and order", () => {
  it("every state has a tone AND a word (colour never travels alone)", () => {
    const states: AttentionItem["state"][] = [
      "expired",
      "overdue",
      "urgent",
      "open",
      "upcoming",
      "done",
    ];
    expect(states.map((state) => attentionTone(item({ state })))).toEqual([
      "strong",
      "strong",
      "warning",
      "warning",
      "info",
      "success",
    ]);
    expect(states.map((state) => stateWord(item({ state })))).toEqual([
      "Expired",
      "Overdue",
      "Urgent",
      "Open",
      "Upcoming",
      "Done",
    ]);
  });

  it("sorts worst first, then soonest, and keeps ties in input order", () => {
    const sorted = sortAttention([
      item({ label: "A", state: "done" }),
      item({ label: "B", state: "urgent", days: 20 }),
      item({ label: "C", state: "expired", days: -2 }),
      item({ label: "D", state: "urgent", days: 5 }),
    ]);
    expect(sorted.map((i) => i.label)).toEqual(["C", "D", "B", "A"]);
  });

  it("shortDate drops the year only within the current one, refuses garbage", () => {
    expect(shortDate("2026-11-14", NOW)).toBe("14 Nov");
    expect(shortDate("2027-01-15", NOW)).toBe("15 Jan 2027");
    expect(shortDate("not-a-date", NOW)).toBeNull();
    expect(shortDate(null, NOW)).toBeNull();
  });
});

describe("buildLedger — area labels and severity over area", () => {
  const labelsOf = (l: ReturnType<typeof buildLedger>) =>
    [...l.rows, ...l.more].map((r) => r.area);

  it("GUILT: company-subject LKPM and company SPT items are Company and Tax, never Family", () => {
    const ledger = buildLedger(
      [
        item({ days: 90, date: "2027-02-01" }),
        item({
          kind: "lkpm",
          subject: "company",
          label: "LKPM Q1",
          state: "done",
        }),
        item({
          kind: "lkpm",
          subject: "company",
          label: "LKPM Q2",
          state: "done",
        }),
        item({
          kind: "spt_company",
          subject: "company",
          label: "SPT co",
          state: "done",
        }),
        item({
          kind: "spt_company",
          subject: "company",
          label: "SPT co 2",
          state: "done",
        }),
      ],
      [],
      NOW,
    );
    expect(labelsOf(ledger)).not.toContain("Family");
    expect(ledger.more.map((r) => r.area).sort()).toEqual(["Company", "Tax"]);
  });

  it("INNOCENCE: a family member's permit is still labelled Family", () => {
    const ledger = buildLedger(
      [item({ subject: "family_member", subject_name: "Member A", days: 9 })],
      [],
      NOW,
    );
    expect(labelsOf(ledger)).toEqual(["Family"]);
  });

  it("GUILT: an expired family permit is visible even when calm Company and Tax rows exist, and a calm headline gives way to it", () => {
    const ledger = buildLedger(
      [
        item({ state: "done", days: 400, date: "2027-12-01" }),
        item({
          kind: "lkpm",
          subject: "company",
          label: "LKPM Q3",
          state: "done",
        }),
        item({ kind: "spt_personal", label: "SPT 2025", state: "done" }),
        item({
          label: "Spouse KITAS",
          subject: "family_member",
          subject_name: "Member A",
          state: "expired",
          days: -4,
          date: "2026-10-30",
        }),
      ],
      [],
      NOW,
    );
    const visible = ledger.rows.map((r) => r.title);
    expect(
      visible.some((t) => t.includes("Member A") && t.includes("expired")),
    ).toBe(true);
    expect(ledger.rows).toHaveLength(3);
    expect(ledger.more).toHaveLength(1);
  });

  it("every expired or overdue row is visible even past the three slots", () => {
    const ledger = buildLedger(
      [
        item({ state: "expired", days: -1, date: "2026-11-02" }),
        item({
          kind: "lkpm",
          subject: "company",
          label: "LKPM",
          state: "overdue",
          date: "2026-10-31",
        }),
        item({
          kind: "spt_personal",
          label: "SPT",
          state: "overdue",
          date: "2026-10-31",
        }),
        item({
          kind: "passport",
          label: "Passport",
          state: "expired",
          days: -3,
          date: "2026-10-31",
        }),
      ],
      [],
      NOW,
    );
    expect(ledger.rows).toHaveLength(4);
    expect(ledger.more).toHaveLength(0);
  });
});

describe("buildLedger — rejected documents never become a Valid visa row", () => {
  it("GUILT: only a rejected KITAS with a future expiry yields no Visa row", () => {
    const ledger = buildLedger(
      [],
      [
        {
          id: 1,
          client_id: 7,
          document_type: "kitas",
          document_category: "immigration",
          status: "rejected",
          expiry_date: "2030-01-01",
        },
      ],
      NOW,
    );
    expect([...ledger.rows, ...ledger.more].map((r) => r.area)).not.toContain(
      "Visa",
    );
  });

  it("INNOCENCE: the same document, verified, yields a Visa row", () => {
    const ledger = buildLedger(
      [],
      [
        {
          id: 1,
          client_id: 7,
          document_type: "kitas",
          document_category: "immigration",
          status: "verified",
          expiry_date: "2030-01-01",
        },
      ],
      NOW,
    );
    expect([...ledger.rows, ...ledger.more].map((r) => r.area)).toContain(
      "Visa",
    );
  });
});
