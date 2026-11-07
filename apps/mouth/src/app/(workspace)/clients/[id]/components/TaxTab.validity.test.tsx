/**
 * Tax (rows 29, 30): a rejected filing does not close the year and a
 * rejected Coretax or EFIN document is not on file; the live twin with the
 * same fields still counts. The year and the SPT clock follow the card's day.
 * Synthetic fixtures only.
 */
import React from "react";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { coretaxStatus } from "./coretax";
import { sptFilings, sptStatus } from "./tax-deadlines";

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({ email: "tester@example.test" }),
    crm: { updateClient: vi.fn() },
  },
}));
const { mockGetClientHistory, mockGetClientReceipts } = vi.hoisted(() => ({
  mockGetClientHistory: vi.fn(),
  mockGetClientReceipts: vi.fn(),
}));
vi.mock("@/lib/api/workspace/lkpm.api", () => ({
  lkpmApi: {
    getClientHistory: mockGetClientHistory,
    getClientReceipts: mockGetClientReceipts,
  },
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const doc = (over: Partial<ClientDocument>): ClientDocument => ({
  id: Math.floor(Math.random() * 1e6),
  client_id: 7,
  document_type: "other",
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockGetClientHistory.mockResolvedValue({ items: [] });
  mockGetClientReceipts.mockResolvedValue({ items: [] });
});

const VOIDS = [
  ["rejected", { status: "rejected" }],
  ["removed", { deleted_at: "2026-10-01T10:00:00Z" }],
  ["archived", { is_archived: true }],
  ["deleted status", { status: "deleted" }],
] as const;

describe("sptFilings (row 29)", () => {
  it.each(VOIDS)("GUILT: a %s SPT does not close the year", (_n, fields) => {
    expect(
      sptFilings(
        [
          doc({
            document_type: "spt_personal",
            fiscal_year: 2025,
            ...fields,
          } as never),
        ],
        "spt_personal",
      ),
    ).toEqual([]);
  });

  it("INNOCENCE: the live twin with the same year closes it", () => {
    const filings = sptFilings(
      [
        doc({
          document_type: "spt_personal",
          fiscal_year: 2025,
          status: "rejected",
        }),
        doc({
          document_type: "spt_personal",
          fiscal_year: 2025,
          status: "received",
        }),
      ],
      "spt_personal",
    );
    expect(filings).toHaveLength(1);
    expect(filings[0].fiscalYear).toBe(2025);
  });

  it("INNOCENCE: statuses that are not rejected or deleted stay live", () => {
    for (const status of [
      "active",
      "google_drive",
      "expired",
      "archived",
      "verified",
    ]) {
      expect(
        sptFilings(
          [
            doc({
              document_type: "spt_personal",
              fiscal_year: 2025,
              status: status as never,
            }),
          ],
          "spt_personal",
        ),
      ).toHaveLength(1);
    }
  });
});

describe("coretaxStatus (row 30)", () => {
  it.each(VOIDS)(
    "GUILT: a %s access or EFIN document is not on file",
    (_n, fields) => {
      const status = coretaxStatus([
        doc({
          document_type: "coretax_access",
          issue_date: "2026-02-10",
          ...fields,
        } as never),
        doc({ document_type: "efin", ...fields } as never),
      ]);
      expect(status.coretaxAccess).toEqual({ onFile: false, date: null });
      expect(status.efin).toEqual({ onFile: false, date: null });
    },
  );

  it("INNOCENCE: the live twin is on file, with its date", () => {
    const status = coretaxStatus([
      doc({
        document_type: "coretax_access",
        issue_date: "2026-02-10",
        status: "rejected",
      }),
      doc({ document_type: "coretax_access", issue_date: "2026-02-10" }),
      doc({ document_type: "efin" }),
    ]);
    expect(status.coretaxAccess).toEqual({ onFile: true, date: "2026-02-10" });
    expect(status.efin.onFile).toBe(true);
  });

  it("the newest date comes from the live documents only", () => {
    const status = coretaxStatus([
      doc({
        document_type: "coretax_access",
        issue_date: "2026-09-01",
        status: "rejected",
      }),
      doc({ document_type: "coretax_access", issue_date: "2026-02-10" }),
    ]);
    expect(status.coretaxAccess.date).toBe("2026-02-10");
  });
});

async function mount(
  documents: ClientDocument[],
  extra: { day?: string; now?: Date } = {},
) {
  const { TaxTab } = await import("./TaxTab");
  return render(
    <TaxTab
      clientId={7}
      formatDate={(d) => d}
      client={{ tax_consultant: "dea.tax@example.test" } as never}
      documents={documents}
      taxConsultants={[]}
      day={extra.day}
      now={extra.now}
    />,
  );
}

describe("TaxTab — the SPT blocks and Coretax box", () => {
  const NOW = new Date(Date.UTC(2026, 2, 21, 4, 0, 0));

  it("GUILT: a rejected SPT does not flip the section to Filed", async () => {
    await mount(
      [
        doc({
          document_type: "spt_personal",
          fiscal_year: 2025,
          status: "rejected",
        }),
      ],
      { now: NOW },
    );
    expect(
      await screen.findByTestId("spt-block-spt_personal"),
    ).not.toHaveAttribute("data-state", "done");
  });

  it("INNOCENCE: a live SPT with the same year and company_id shows Filed", async () => {
    await mount(
      [
        doc({
          document_type: "spt_personal",
          fiscal_year: 2025,
          company_id: 3,
        } as Partial<ClientDocument>),
      ],
      { now: NOW },
    );
    const block = await screen.findByTestId("spt-block-spt_personal");
    expect(block).toHaveAttribute("data-state", "done");
    expect(within(block).getByText("Filed")).toBeInTheDocument();
  });

  it("GUILT: a rejected access or EFIN document shows 'No ... on file'", async () => {
    await mount(
      [
        doc({
          document_type: "coretax_access",
          issue_date: "2026-02-10",
          status: "rejected",
        }),
        doc({ document_type: "efin", status: "rejected" }),
      ],
      { now: NOW },
    );
    expect(
      await screen.findByText("No Coretax access on file"),
    ).toBeInTheDocument();
  });

  it("INNOCENCE: a live one shows '... on file' with its date", async () => {
    await mount(
      [
        doc({ document_type: "coretax_access", issue_date: "2026-02-10" }),
        doc({ document_type: "efin" }),
      ],
      { now: NOW },
    );
    const box = await screen.findByTestId("coretax-access");
    expect(box).toHaveTextContent("Coretax access on file · 2026-02-10");
    expect(within(box).getByText("EFIN on file")).toBeInTheDocument();
  });
});

describe("TaxTab — the SPT path takes the card's day (row 29, obligation 4)", () => {
  it("with the browser year past the card's year, the selector, its default and the SPT status use the card's year", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2027-06-15T00:00:00Z"));
    try {
      await mount([], { day: "2026-03-30" });
      // The selector lists the card's year first, never the browser's.
      const years = screen
        .getAllByRole("button", { name: /^20\d\d$/ })
        .map((b) => b.textContent);
      expect(years[0]).toBe("2026");
      expect(years).not.toContain("2027");
      // The default selected year is the card's year.
      expect(screen.getByRole("button", { name: "2026" })).toHaveClass(
        "border-[var(--tx-pure)]",
      );
      // 30 March 2026: the personal SPT (31 March) is urgent for FY 2025,
      // not already overdue as it would be on the browser's 2027 date.
      expect(
        await screen.findByTestId("spt-block-spt_personal"),
      ).toHaveAttribute("data-state", "urgent");
    } finally {
      vi.useRealTimers();
    }
  });

  it("when the next day arrives the SPT status changes with it", async () => {
    const first = await mount([], { day: "2026-03-31" });
    expect(await screen.findByTestId("spt-block-spt_personal")).toHaveAttribute(
      "data-state",
      "urgent",
    );
    first.unmount();
    await mount([], { day: "2026-04-01" });
    expect(await screen.findByTestId("spt-block-spt_personal")).toHaveAttribute(
      "data-state",
      "overdue",
    );
  });

  it("sptStatus has no default clock: the day is always handed in", () => {
    const status = sptStatus(
      "spt_personal",
      new Set(),
      new Date("2026-03-31T00:00:00+08:00"),
    );
    expect(status.state).toBe("urgent");
    expect(status.daysToDeadline).toBe(0);
    expect(
      sptStatus(
        "spt_personal",
        new Set(),
        new Date("2026-04-01T00:00:00+08:00"),
      ).state,
    ).toBe("overdue");
  });
});
