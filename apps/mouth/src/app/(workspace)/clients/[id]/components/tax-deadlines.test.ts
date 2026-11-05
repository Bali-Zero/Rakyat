// SPT deadline ladder — every boundary, personal (31 Mar) and company (30 Apr).
// `now` is injected; the instants are chosen so that the WITA calendar day
// differs from the UTC one where it matters. Synthetic data only.
import { describe, expect, it } from "vitest";
import {
  documentFiscalYear,
  sptFilings,
  sptStatus,
  type SptKind,
  type SptState,
} from "./tax-deadlines";

/** Noon WITA on the given 2026 calendar day (04:00 UTC). */
const at = (month: number, day: number) =>
  new Date(Date.UTC(2026, month - 1, day, 4, 0, 0));
const NONE = new Set<number>();
const FILED = new Set<number>([2025]);

const LADDER: [SptKind, number, number][] = [
  ["spt_personal", 3, 31],
  ["spt_company", 4, 30],
];

describe.each(LADDER)("%s deadline %i/%i", (kind, month, day) => {
  const state = (offsetDays: number, filed = NONE) => {
    const d = at(month, day);
    d.setUTCDate(d.getUTCDate() - offsetDays);
    return sptStatus(kind, filed, d);
  };

  it("targets fiscal year Y-1", () => {
    expect(state(0).fiscalYear).toBe(2025);
  });

  it.each<[number, SptState]>([
    [60, "none"],
    [31, "none"], // deadline-31: quiet
    [30, "upcoming"], // countdown starts
    [11, "upcoming"],
    [10, "urgent"], // alarm starts
    [1, "urgent"],
    [0, "urgent"], // the deadline day itself
    [-1, "overdue"],
    [-30, "overdue"], // last overdue day
    [-31, "none"], // the alarm ends
    [-200, "none"],
  ])("%i days before the deadline reads %s", (offset, expected) => {
    expect(state(offset).state).toBe(expected);
  });

  it("a filing for Y-1 on file is done at every point of the year", () => {
    for (const offset of [60, 30, 10, 0, -1, -31, -200]) {
      expect(state(offset, FILED).state).toBe("done");
    }
    expect(state(0, FILED).label).toBe("Filed");
  });

  it("a filing for another year does not count", () => {
    expect(state(0, new Set([2024])).state).toBe("urgent");
    expect(state(0, new Set([2026])).state).toBe("urgent");
  });

  it("31 Dec is quiet and 1 Jan targets the new fiscal year", () => {
    const s = sptStatus(kind, NONE, at(12, 31));
    expect(s.state).toBe("none");
    expect(s.isNext).toBe(true);
    expect(s.label).toMatch(/^opens .* 2027$/);
    const jan = sptStatus(kind, NONE, new Date(Date.UTC(2027, 0, 1, 4)));
    expect(jan.state).toBe("none");
    expect(jan.fiscalYear).toBe(2026);
    expect(jan.isNext).toBe(false);
    expect(jan.missingYear).toBeNull();
  });
});

describe("labels", () => {
  it("personal: countdown, alarm, overdue and quiet wording", () => {
    expect(sptStatus("spt_personal", NONE, at(3, 1)).label).toBe(
      "Due in 30 days · 31 Mar",
    );
    expect(sptStatus("spt_personal", NONE, at(3, 1)).state).toBe("upcoming");
    expect(sptStatus("spt_personal", NONE, at(3, 30)).label).toBe(
      "Closes 31 Mar · 1 day",
    );
    expect(sptStatus("spt_personal", NONE, at(3, 31)).label).toBe(
      "Closes 31 Mar · today",
    );
    expect(sptStatus("spt_personal", NONE, at(4, 3)).label).toBe(
      "Overdue by 3 days · 31 Mar",
    );
    expect(sptStatus("spt_personal", NONE, at(2, 1)).label).toBe(
      "Next filing opens 1 Mar 2026",
    );
  });

  it("company: 30 Apr, countdown opens 31 Mar", () => {
    expect(sptStatus("spt_company", NONE, at(3, 1)).label).toBe(
      "Next filing opens 31 Mar 2026",
    );
    expect(sptStatus("spt_company", NONE, at(4, 20)).label).toBe(
      "Closes 30 Apr · 10 days",
    );
  });
});

describe("WITA calendar days", () => {
  it("23:30 UTC on 30 Mar is already 31 Mar in Bali", () => {
    const s = sptStatus(
      "spt_personal",
      NONE,
      new Date(Date.UTC(2026, 2, 30, 23, 30)),
    );
    expect(s.daysToDeadline).toBe(0);
    // Guilt/innocence pair: 15:30 UTC is still 30 Mar in Bali.
    expect(
      sptStatus("spt_personal", NONE, new Date(Date.UTC(2026, 2, 30, 15, 30)))
        .daysToDeadline,
    ).toBe(1);
  });
});

describe("filings from documents", () => {
  it("uses fiscal_year first, then a year in the file name, else ignores", () => {
    expect(
      documentFiscalYear({ document_type: "spt", fiscal_year: 2024 }),
    ).toBe(2024);
    expect(
      documentFiscalYear({ document_type: "spt", file_name: "SPT-2023-x.pdf" }),
    ).toBe(2023);
    expect(
      documentFiscalYear({ document_type: "spt", file_name: "id12025999.pdf" }),
    ).toBeNull();
    expect(documentFiscalYear({ document_type: "spt" })).toBeNull();
  });

  it("matches the type per kind, newest year first, drops archived/removed", () => {
    const docs = [
      { document_type: "spt_personal", fiscal_year: 2023 },
      { document_type: "spt_personal", fiscal_year: 2025, file_url: "u25" },
      { document_type: "spt", fiscal_year: 2024 },
      { document_type: "spt_personal", fiscal_year: 2022, is_archived: true },
      {
        document_type: "spt_personal",
        fiscal_year: 2021,
        deleted_at: "2026-01-01",
      },
      { document_type: "passport", fiscal_year: 2025 },
    ];
    expect(sptFilings(docs, "spt_personal").map((f) => f.fiscalYear)).toEqual([
      2025, 2023,
    ]);
    expect(sptFilings(docs, "spt_personal")[0].url).toBe("u25");
    expect(sptFilings(docs, "spt_company").map((f) => f.fiscalYear)).toEqual([
      2024,
    ]);
  });
});

describe("after the window: the line points at the next filing", () => {
  const cases: [SptKind, number, number, number, number, string, string][] = [
    // kind, last overdue month/day, first quiet month/day, next-year, , opens
    ["spt_personal", 4, 30, 5, 1, "opens 1 Mar 2027", "FY 2025"],
    ["spt_company", 5, 30, 5, 31, "opens 31 Mar 2027", "FY 2025"],
  ];
  it.each(cases)(
    "%s: last overdue day keeps the past year, the day after flips to next",
    (kind, lm, ld, nm, nd, opens) => {
      const lastOverdue = sptStatus(kind, NONE, at(lm, ld));
      expect(lastOverdue.state).toBe("overdue");
      expect(lastOverdue.fiscalYear).toBe(2025);
      expect(lastOverdue.isNext).toBe(false);
      expect(lastOverdue.missingYear).toBeNull();
      const next = sptStatus(kind, NONE, at(nm, nd));
      expect(next.state).toBe("none");
      expect(next.isNext).toBe(true);
      expect(next.fiscalYear).toBe(2026);
      expect(next.missingYear).toBe(2025);
      expect(next.label).toBe(opens);
    },
  );
  it("filed: stays done on the past year, no missing year", () => {
    const s = sptStatus("spt_personal", FILED, at(8, 1));
    expect(s.state).toBe("done");
    expect(s.fiscalYear).toBe(2025);
    expect(s.missingYear).toBeNull();
  });
  it("before the window (January) it stays on the current year with a full date", () => {
    const s = sptStatus("spt_personal", NONE, at(1, 15));
    expect(s.isNext).toBe(false);
    expect(s.missingYear).toBeNull();
    expect(s.fiscalYear).toBe(2025);
    expect(s.label).toBe("Next filing opens 1 Mar 2026");
  });
});

describe("sptFilings — a rejected filing is not a filing", () => {
  it("GUILT: a rejected SPT is not counted; INNOCENCE: a verified one is", () => {
    const base = { document_type: "spt_personal", fiscal_year: 2025 };
    expect(
      sptFilings([{ ...base, status: "rejected" }], "spt_personal"),
    ).toEqual([]);
    expect(
      sptFilings([{ ...base, status: "verified" }], "spt_personal").map(
        (f) => f.fiscalYear,
      ),
    ).toEqual([2025]);
  });
});
