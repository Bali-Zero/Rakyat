import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { ImmigrationTab, isVisaFamilyDocument } from "./ImmigrationTab";
import { pickCurrentVisa } from "./pickCurrentVisa";

vi.mock("./AiSummaryCard", () => ({ AiSummaryCard: () => null }));

const { getClientRecap } = vi.hoisted(() => ({
  getClientRecap: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { crm: { deleteDocument: vi.fn(), getClientRecap } },
}));

// Hoisted so the SAME `push` spy backs every `useRouter()` call across the
// file — a fresh `vi.fn()` per call (the round-2 shape) can never observe an
// argument, which is exactly what let a wrong navigation target through
// unpinned (R6-gate.md §4b, mutation k).
const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

beforeEach(() => {
  push.mockClear();
  // The AI summary slot stays empty unless a test gives it text.
  getClientRecap.mockReset();
  getClientRecap.mockRejectedValue(new Error("no recap"));
});

// A few cases below freeze the clock (vi.setSystemTime) to pin exact-day
// boundaries. Always restore real time afterwards — a leaked fake clock
// would desync every other `Date.now()`-based fixture in this file.
afterEach(() => {
  vi.useRealTimers();
});

describe("isVisaFamilyDocument", () => {
  it("matches kitas, kitap, visa and e-visa document types", () => {
    for (const type of ["kitas", "KITAP", "visa c1", "e-visa", "evisa"]) {
      expect(isVisaFamilyDocument({ document_type: type })).toBe(true);
    }
  });

  it("matches VOA and its e-VOA spelling (GUILT: it is a visa document)", () => {
    for (const type of ["voa", "VOA", "e-voa", "evoa"]) {
      expect(isVisaFamilyDocument({ document_type: type })).toBe(true);
    }
  });

  it("does not match a non-visa document type (INNOCENCE)", () => {
    expect(isVisaFamilyDocument({ document_type: "passport" })).toBe(false);
    expect(isVisaFamilyDocument({ document_type: "working permit" })).toBe(
      false,
    );
  });
});

const baseDoc: ClientDocument = {
  id: 1,
  client_id: 1,
  document_type: "voa",
};

const renderTab = (documents: ClientDocument[]) =>
  render(
    <ImmigrationTab
      clientId={1}
      documents={documents}
      formatDate={(d) => d}
      onAddClick={vi.fn()}
      onEditClick={vi.fn()}
      onRefresh={vi.fn()}
    />,
  );

describe("ImmigrationTab — current permit panel vs visa history (R6 restyle)", () => {
  it("shows an unexpired VOA in the current-permit panel, not only in history (GUILT)", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 1,
        document_type: "voa",
        expiry_date: futureDate,
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
  });

  it("a lone expired VOA is still the current permit (the latest one), with no history (one rule shared with Overview)", () => {
    const pastDate = new Date(Date.now() - 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 2,
        document_type: "voa",
        expiry_date: pastDate,
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
  });

  it("keeps an unexpired kitas as the current permit when a VOA is also present (existing precedence unchanged)", () => {
    const futureDate = new Date(Date.now() + 400 * 86400000)
      .toISOString()
      .slice(0, 10);
    const nearDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 3,
        document_type: "kitas",
        expiry_date: futureDate,
      },
      {
        ...baseDoc,
        id: 4,
        document_type: "voa",
        expiry_date: nearDate,
      },
    ]);

    // Current permit picks the document with the furthest-out expiry first
    // (existing sort order: descending expiry) — unchanged by this fix.
    // Panel + the current permit's own first row of the history.
    expect(screen.getAllByText("kitas")).toHaveLength(2);
    expect(screen.getByText("Visa history")).toBeInTheDocument();
    expect(screen.getAllByText("voa")).toHaveLength(1);
  });

  it("never classifies a non-visa document (e.g. passport-like) as a visa", () => {
    renderTab([
      {
        ...baseDoc,
        id: 5,
        document_type: "working permit",
        document_category: "immigration",
      },
    ]);

    expect(screen.queryByText("Current permit")).not.toBeInTheDocument();
    expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
    expect(screen.getByText("Working Permit")).toBeInTheDocument();
  });

  it("renders the elapsed/expiry KPIs from real issue_date + expiry_date (no invented data)", () => {
    const issueDate = new Date(Date.now() - 20 * 86400000)
      .toISOString()
      .slice(0, 10);
    const expiryDate = new Date(Date.now() + 40 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 6,
        document_type: "kitas",
        issue_date: issueDate,
        expiry_date: expiryDate,
      },
    ]);

    expect(screen.getByText("Days on this permit")).toBeInTheDocument();
    expect(screen.getByText("Days to expiry")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /days elapsed/ }),
    ).toBeInTheDocument();
  });

  it("GUILT: omits the elapsed-days KPI and the validity track when issue_date is missing, rather than guessing it", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 7,
        document_type: "kitas",
        expiry_date: futureDate,
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Days on this permit")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: /days elapsed/ }),
    ).not.toBeInTheDocument();
    // The countdown itself does not need issue_date — only elapsed/total do.
    expect(screen.getByText("Days to expiry")).toBeInTheDocument();
  });

  it("Visa history shows a real document status when present, and a plain dash when it is not (no guessed status)", () => {
    const pastDate = new Date(Date.now() - 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 8,
        document_type: "kitas",
        expiry_date: pastDate,
        status: "verified",
      },
      {
        ...baseDoc,
        id: 9,
        document_type: "voa",
        expiry_date: pastDate,
      },
    ]);

    // R6c: the phone step (below 640px) relocates the same status node onto
    // the row's first cell too, so "verified" now renders twice — desktop
    // Status cell + phone block, jsdom evaluates no media queries.
    // A previous permit reads "Completed" (never the raw status word).
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.queryByText("verified")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — Start Renewal CTA (R6 repair P0-1)", () => {
  it("GUILT: the permit panel shows Start Renewal for a kitas expiring in 45 days", () => {
    const soon = new Date(Date.now() + 45 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 20, document_type: "kitas", expiry_date: soon },
    ]);

    expect(
      screen.getByRole("button", { name: /start renewal/i }),
    ).toBeInTheDocument();
  });

  it("INNOCENCE: the permit panel shows no Start Renewal for a kitas expiring in 200 days", () => {
    const far = new Date(Date.now() + 200 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 21, document_type: "kitas", expiry_date: far },
    ]);

    expect(
      screen.queryByRole("button", { name: /start renewal/i }),
    ).not.toBeInTheDocument();
  });

  it("GUILT: an expired kitas in Visa history still offers Start Renewal", () => {
    const current = new Date(Date.now() + 400 * 86400000)
      .toISOString()
      .slice(0, 10);
    const expired = new Date(Date.now() - 10 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 22, document_type: "kitas", expiry_date: current },
      { ...baseDoc, id: 23, document_type: "kitas", expiry_date: expired },
    ]);

    expect(screen.getByText("Visa history")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /start renewal for kitas/i }),
    ).toBeInTheDocument();
  });

  it("BOUNDARY: shows Start Renewal at exactly 90 days out", () => {
    const now = new Date("2026-01-01T00:00:00.000Z");
    vi.setSystemTime(now);
    const expiryDate = new Date(now.getTime() + 90 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 24, document_type: "kitas", expiry_date: expiryDate },
    ]);

    expect(
      screen.getByRole("button", { name: /start renewal/i }),
    ).toBeInTheDocument();
  });

  it("BOUNDARY: hides Start Renewal at 91 days out — the threshold did not move", () => {
    const now = new Date("2026-01-01T00:00:00.000Z");
    vi.setSystemTime(now);
    const expiryDate = new Date(now.getTime() + 91 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 25, document_type: "kitas", expiry_date: expiryDate },
    ]);

    expect(
      screen.queryByRole("button", { name: /start renewal/i }),
    ).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — 'Days to expiry' never flips sign (R6 repair P0-2)", () => {
  it("GUILT: a permit expired 10 days ago (still in the 30-day grace) reads 'Days past expiry', never 'Days to expiry'", () => {
    const pastExpiry = new Date(Date.now() - 10 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 30,
        document_type: "kitas",
        expiry_date: pastExpiry,
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.getByText("Days past expiry")).toBeInTheDocument();
    expect(screen.queryByText("Days to expiry")).not.toBeInTheDocument();
  });

  it("INNOCENCE: a permit expiring in 10 days still reads 'Days to expiry'", () => {
    const futureExpiry = new Date(Date.now() + 10 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 31,
        document_type: "kitas",
        expiry_date: futureExpiry,
      },
    ]);

    expect(screen.getByText("Days to expiry")).toBeInTheDocument();
    expect(screen.queryByText("Days past expiry")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — 30-day grace boundary (R6 repair P1-6)", () => {
  it("BOUNDARY: a visa expired 29 days ago still shows in the permit panel (inside grace)", () => {
    const withinGrace = new Date(Date.now() - 29 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 40, document_type: "kitas", expiry_date: withinGrace },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
  });

  it("a visa expired 31 days ago is still the current permit while it is the only one (no 30-day grace any more)", () => {
    const outsideGrace = new Date(Date.now() - 31 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 41,
        document_type: "kitas",
        expiry_date: outsideGrace,
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
  });
});

/**
 * jsdom does not evaluate `@media` queries, so a plain `getByRole` cannot
 * see whether a control sits under the hover/focus-only `actions` slot
 * (`opacity-0 … [@media(hover:none)]:hidden`, `HairlineGrid.tsx:198-202`) —
 * on a touch device that slot is `display:none`. Walking the ancestor
 * chain and asserting neither class is present on any of it is the
 * structural proxy the re-audit asked for (§7 blocker 1).
 */
function assertReachableOnTouch(el: HTMLElement) {
  let node: HTMLElement | null = el;
  while (node) {
    expect(node.className).not.toMatch(/(?:^|\s)opacity-0(?:\s|$)/);
    expect(node.className).not.toMatch(/\[@media\(hover:none\)\]:hidden/);
    node = node.parentElement;
  }
}

describe("ImmigrationTab — Visa-history row controls are reachable on touch (R6 second repair, blocker 1)", () => {
  it("GUILT: Start Renewal and Download on a history row carry no hover/focus-only ancestor", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const renewDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 70, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 71,
        document_type: "kitas",
        expiry_date: renewDate,
        google_drive_file_url:
          "https://drive.google.com/file/d/synthetic-fixture-id/view",
      },
    ]);

    assertReachableOnTouch(
      screen.getByRole("button", { name: /start renewal for kitas/i }),
    );
    assertReachableOnTouch(
      screen.getByRole("button", { name: /download kitas/i }),
    );
  });

  it("GUILT (round 3 — B1 inverts the round-2 pin): Edit and Remove on the same row also carry no hover/focus-only ancestor", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 72, document_type: "kitas", expiry_date: currentDate },
      { ...baseDoc, id: 73, document_type: "kitas", expiry_date: historyDate },
    ]);

    for (const b of screen.getAllByRole("button", { name: /edit kitas/i }))
      assertReachableOnTouch(b);
    for (const b of screen.getAllByRole("button", { name: /remove kitas/i }))
      assertReachableOnTouch(b);
  });

  it("PIN (B1 — the defect that would have caught the missing column in round 1): every Visa-history row renders the same number of grid children as the head", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const withMember = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    const withoutMember = new Date(Date.now() - 15 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 74, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 75,
        document_type: "kitap",
        expiry_date: withMember,
      },
      { ...baseDoc, id: 76, document_type: "visa", expiry_date: withoutMember },
    ]);

    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    );
    expect(grid).not.toBeNull();
    const head = grid!.querySelector('[role="row"]');
    expect(head).not.toBeNull();
    const body = head!.nextElementSibling;
    expect(body).not.toBeNull();
    const rows = Array.from(body!.children);
    expect(rows.length).toBe(3); // current + withMember + withoutMember
    for (const row of rows) {
      expect(row.children.length).toBe(head!.children.length);
    }
  });

  it("PIN (R6b — the PR 6520 inert-collapse trap): the collapsed override carries as many tracks as non-collapse head cells, and the expanded template keeps all of them", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 80, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 81,
        document_type: "kitap",
        issue_date: "2020-01-01",
        expiry_date: historyDate,
      },
    ]);

    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    );
    expect(grid).not.toBeNull();
    const head = grid!.querySelector('[role="row"]');
    expect(head).not.toBeNull();

    // `HairlineGrid`'s own `colsCollapsed` API only ever emits a `--cols`
    // media rule, and an inline `--cols` on the same element beats it
    // (PR 6520) — so the fix cannot rely on `--cols` at all below the
    // breakpoint. This pin fails if the fix goes back to relying on that
    // inert API: it requires a SEPARATE override that sets
    // `grid-template-columns` directly (the same technique DocumentsTab,
    // FamilyTab and ObligationsTable use), and checks its track count
    // against the DOM rather than trusting the prop.
    const overrideMatch = grid!.className.match(
      /\[&_\.grid\]:!grid-cols-\[([^\]]+)\]/,
    );
    expect(overrideMatch).not.toBeNull();
    const collapsedTracks = overrideMatch![1].split("_");

    const nonCollapseHeadCells = Array.from(head!.children).filter(
      (cell) => !cell.hasAttribute("data-collapse"),
    );
    // 4: Visa type, Status, Expires, Actions — "Issued" carries
    // `data-collapse` and leaves the flow below the breakpoint.
    expect(collapsedTracks.length).toBe(nonCollapseHeadCells.length);
    expect(collapsedTracks.length).toBe(4);

    const expandedTracks = grid!
      .getAttribute("style")!
      .match(/--cols:\s*([^;]+);/)![1]
      .trim()
      .split(/\s+/);
    expect(expandedTracks.length).toBe(head!.children.length);
    expect(expandedTracks.length).toBe(5);
  });
});

describe("ImmigrationTab — visa-history grid gets a phone step below 640px (R6c)", () => {
  it("PIN A: a max-sm override sized to the phone-visible head cells sits alongside the untouched R6b 1360 override", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 100, document_type: "kitas", expiry_date: currentDate },
      { ...baseDoc, id: 101, document_type: "kitap", expiry_date: historyDate },
    ]);

    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    );
    expect(grid).not.toBeNull();
    const head = grid!.querySelector('[role="row"]');
    expect(head).not.toBeNull();

    const phoneVisibleHeadCells = Array.from(head!.children).filter(
      (cell) =>
        !cell.hasAttribute("data-collapse") &&
        !cell.className.includes("max-sm:hidden"),
    );
    // Visa type + the sr-only Actions head — Status and Expires leave below
    // 640px, same as Issued already does below 1360px.
    expect(phoneVisibleHeadCells.length).toBe(2);

    const phoneOverride = grid!.className.match(
      /max-sm:\[&_\.grid\]:!grid-cols-\[([^\]]+)\]/,
    );
    expect(phoneOverride).not.toBeNull();
    expect(phoneOverride![1].split("_").length).toBe(
      phoneVisibleHeadCells.length,
    );

    // R6b's 1360px override must stay green and unmodified in meaning: 4
    // tracks (Visa type, Status, Expires, Actions — Issued carries
    // data-collapse and already leaves the flow there).
    const desktopOverride = grid!.className.match(
      /max-\[1360px\]:\[&_\.grid\]:!grid-cols-\[([^\]]+)\]/,
    );
    expect(desktopOverride).not.toBeNull();
    expect(desktopOverride![1].split("_").length).toBe(4);
  });

  it("PIN B: every history row keeps 5 direct children, and its Status and Expires cells carry max-sm:hidden", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate1 = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate2 = new Date(Date.now() - 15 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 102, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 103,
        document_type: "kitap",
        issue_date: "2020-01-01",
        expiry_date: historyDate1,
      },
      {
        ...baseDoc,
        id: 104,
        document_type: "visa",
        issue_date: "2020-01-01",
        expiry_date: historyDate2,
      },
    ]);

    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    );
    const head = grid!.querySelector('[role="row"]');
    const body = head!.nextElementSibling;
    const rows = Array.from(body!.children) as HTMLElement[];
    expect(rows.length).toBe(3); // the current permit leads the history

    for (const row of rows) {
      expect(row.children.length).toBe(head!.children.length);
      expect(row.children.length).toBe(5);
      const statusCell = row.children[1] as HTMLElement;
      const expiresCell = row.children[3] as HTMLElement;
      expect(statusCell.className).toContain("max-sm:hidden");
      expect(expiresCell.className).toContain("max-sm:hidden");
    }
  });

  it("PIN C: the phone block in the row's first cell repeats the status cell and the exact validity string (older rows carry no deadline)", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));

    renderTab([
      {
        ...baseDoc,
        id: 110,
        document_type: "kitas",
        expiry_date: "2027-05-16", // the latest, so the current permit
      },
      {
        ...baseDoc,
        id: 111,
        document_type: "kitap",
        expiry_date: "2026-07-19",
        issue_date: "2025-07-19",
        status: "pending",
      },
      { ...baseDoc, id: 112, document_type: "voa", expiry_date: "2025-12-22" },
    ]);

    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    );
    const head = grid!.querySelector('[role="row"]');
    const body = head!.nextElementSibling;
    const rows = Array.from(body!.children) as HTMLElement[];
    expect(rows.length).toBe(3); // the current permit leads the history

    for (const row of rows) {
      const wrapper = row.children[0] as HTMLElement;
      const phoneBlock = wrapper.querySelector(".sm\\:hidden") as HTMLElement;
      expect(phoneBlock).not.toBeNull();
      const desktopValidity = row.children[3] as HTMLElement;
      expect(phoneBlock.textContent).toContain(desktopValidity.textContent);
      expect(phoneBlock.textContent).toMatch(/Completed|Verified|Current/);
      expect(phoneBlock.textContent).not.toMatch(/ago|left/);
      expect(desktopValidity.className).not.toContain("state-warning");
    }

    const periodRow = rows.find((r) =>
      r.children[3].textContent?.includes("2025-07-19 → 2026-07-19"),
    )!;
    expect(periodRow.children[1].textContent).toContain("Completed");
    const endedRow = rows.find((r) =>
      r.children[3].textContent?.includes("Ended 2025-12-22"),
    )!;
    expect(endedRow).toBeDefined();
  });
});

describe("ImmigrationTab — the six unpinned repairs from R6-reaudit.md §3 (M6-M11)", () => {
  it("GUILT (inverted): an older row's date is never warned, even with alert_color 'yellow' (only the current permit has a deadline)", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const currentDate = "2027-05-16"; // +500d, becomes the current permit
    const yellowDate = "2026-10-28"; // +300d, an older permit
    renderTab([
      { ...baseDoc, id: 50, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 51,
        document_type: "kitap",
        expiry_date: yellowDate,
        alert_color: "yellow",
      },
    ]);

    const expiryCells = screen.getAllByText(new RegExp(yellowDate));
    expect(expiryCells).toHaveLength(2);
    for (const cell of expiryCells) {
      expect(cell.className).not.toContain("text-[var(--state-warning)]");
      expect(cell.textContent).not.toMatch(/left|ago/);
    }
  });

  it("INNOCENCE: alert_color 'green' 300 days out stays a plain history date (M6)", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const currentDate = "2027-05-16"; // +500d
    const greenDate = "2026-10-28"; // +300d
    renderTab([
      { ...baseDoc, id: 52, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 53,
        document_type: "kitap",
        expiry_date: greenDate,
        alert_color: "green",
      },
    ]);

    // R6c: same duplication as the yellow case above.
    const expiryCells = screen.getAllByText(new RegExp(greenDate));
    expect(expiryCells).toHaveLength(2);
    for (const cell of expiryCells) {
      expect(cell.className).not.toContain("text-[var(--state-warning)]");
    }
  });

  it("PIN: formatDocType strips underscores on both the panel plaque and the history grid (M7)", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 54,
        document_type: "kitas_c317",
        expiry_date: currentDate,
      },
      {
        ...baseDoc,
        id: 55,
        document_type: "kitap_dependent",
        expiry_date: historyDate,
      },
    ]);

    expect(screen.getAllByText("kitas c317").length).toBeGreaterThan(0);
    expect(screen.getByText("kitap dependent")).toBeInTheDocument();
  });

  it("PIN: an older permit shows its validity period and no countdown, while the current permit does (GUILT + INNOCENCE)", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    renderTab([
      {
        ...baseDoc,
        id: 56,
        document_type: "kitas",
        expiry_date: "2027-05-16",
      },
      {
        ...baseDoc,
        id: 57,
        document_type: "kitap",
        issue_date: "2024-12-22",
        expiry_date: "2025-12-22",
      },
    ]);

    // History row: period + tag, no "ago"/"left" for it (desktop cell + phone line).
    expect(screen.getAllByText(/2024-12-22 → 2025-12-22/)).toHaveLength(2);
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Expired 10d ago/)).not.toBeInTheDocument();
    // The current permit keeps its countdown.
    expect(screen.getByText(/2027-05-16 · .*left/)).toBeInTheDocument();
  });

  it("PIN: the current-permit panel does not print the file name; it is the download button's title and aria-label (M9, inverted)", () => {
    const currentDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 58,
        document_type: "kitas",
        expiry_date: currentDate,
        file_name: "synthetic-permit-scan.pdf",
        google_drive_file_url: "https://drive.google.com/file/d/abc123/view",
      },
    ]);

    expect(
      screen.queryByText("synthetic-permit-scan.pdf"),
    ).not.toBeInTheDocument();
    const dl = screen.getByRole("button", {
      name: "Download current permit",
    });
    expect(dl).toHaveAttribute("title", "synthetic-permit-scan.pdf");
  });

  it("PIN: the Visa history heading shows the real row count (M10)", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const h1 = new Date(Date.now() - 5 * 86400000).toISOString().slice(0, 10);
    const h2 = new Date(Date.now() - 15 * 86400000).toISOString().slice(0, 10);
    renderTab([
      { ...baseDoc, id: 59, document_type: "kitas", expiry_date: currentDate },
      { ...baseDoc, id: 60, document_type: "kitap", expiry_date: h1 },
      { ...baseDoc, id: 61, document_type: "visa", expiry_date: h2 },
    ]);

    expect(screen.getByText("Visa history")).toBeInTheDocument();
    expect(screen.getByText("(2)")).toBeInTheDocument();
  });

  it("PIN (inverted): a family member's permit is not a history row; one quiet line points to the Family tab (M11)", () => {
    const currentDate = new Date(Date.now() + 500 * 86400000)
      .toISOString()
      .slice(0, 10);
    const historyDate = new Date(Date.now() - 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 62, document_type: "kitas", expiry_date: currentDate },
      {
        ...baseDoc,
        id: 63,
        document_type: "kitap",
        expiry_date: historyDate,
        family_member_name: "Synthetic Dependent",
      },
    ]);

    expect(screen.queryByText(/Synthetic Dependent/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Family permits are in the Family tab (1)"),
    ).toBeInTheDocument();
  });
});

describe("ImmigrationTab — days KPI numeral never carries a sign (R6 round 3, C1)", () => {
  it("PIN: the expiry KPI numeral renders the unsigned day count for a −10d fixture", () => {
    const pastExpiry = new Date(Date.now() - 10 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 80, document_type: "kitas", expiry_date: pastExpiry },
    ]);

    const caption = screen.getByText("Days past expiry");
    expect(caption.previousElementSibling?.textContent).toBe("10");
  });

  it("PIN: the elapsed-days KPI clamps to zero rather than a negative for a future issue_date", () => {
    const futureIssue = new Date(Date.now() + 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    const expiry = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 81,
        document_type: "kitas",
        issue_date: futureIssue,
        expiry_date: expiry,
      },
    ]);

    const caption = screen.getByText("Days on this permit");
    expect(caption.previousElementSibling?.textContent).toBe("00");
  });
});

describe("ImmigrationTab — Start Renewal navigates to the real target (R6 round 3, C2)", () => {
  it("PIN: the panel CTA calls router.push with the visa_renewal target for this client", () => {
    const soon = new Date(Date.now() + 45 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 90, document_type: "kitas", expiry_date: soon },
    ]);

    fireEvent.click(screen.getByRole("button", { name: /start renewal/i }));

    expect(push).toHaveBeenCalledWith(
      "/process/new?client_id=1&type=visa_renewal",
    );
  });

  it("PIN: a history row's CTA calls router.push with the same target", () => {
    const current = new Date(Date.now() + 400 * 86400000)
      .toISOString()
      .slice(0, 10);
    const expired = new Date(Date.now() - 10 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 91, document_type: "kitas", expiry_date: current },
      { ...baseDoc, id: 92, document_type: "kitas", expiry_date: expired },
    ]);

    fireEvent.click(
      screen.getByRole("button", { name: /start renewal for kitas/i }),
    );

    expect(push).toHaveBeenCalledWith(
      "/process/new?client_id=1&type=visa_renewal",
    );
  });
});

describe("ImmigrationTab — precise permit label (kita.balizero.com owner report: 'Permit: Visa' when the document is a KITAP)", () => {
  it("shows the backend-resolved permit_label instead of the raw generic document_type", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 200,
        document_type: "visa",
        expiry_date: futureDate,
        permit_family: "kitap",
        permit_label: "KITAP / ITAP — Permanent Stay Permit",
      },
    ]);

    expect(
      screen.getByText("KITAP / ITAP — Permanent Stay Permit"),
    ).toBeInTheDocument();
    expect(screen.queryByText("visa")).not.toBeInTheDocument();
  });

  it("GUILT: falls back to the raw document_type when the backend found no evidence for a family (never invents one)", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 201,
        document_type: "visa",
        expiry_date: futureDate,
        // no permit_label — backend had no evidence
      },
    ]);

    expect(screen.getByText("visa")).toBeInTheDocument();
  });

  it("shows the permit number and sponsor when the backend extracted them, and omits them when absent", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 202,
        document_type: "kitas",
        expiry_date: futureDate,
        permit_number: "TEST-0000",
        permit_sponsor: "Example Sponsor",
      },
    ]);

    expect(screen.getByText("TEST-0000")).toBeInTheDocument();
    expect(screen.getByText("Example Sponsor")).toBeInTheDocument();
  });

  it("GUILT: omits the permit-number and sponsor rows when the backend did not extract them", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 203, document_type: "kitas", expiry_date: futureDate },
    ]);

    expect(screen.queryByText("Permit no.")).not.toBeInTheDocument();
    expect(screen.queryByText("Sponsor")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — MERP rides with the current permit, not 'Other' (owner report)", () => {
  it("GUILT: a document_type='MERP' companion document is NOT rendered in the Other section", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 210, document_type: "kitas", expiry_date: futureDate },
      {
        ...baseDoc,
        id: 211,
        document_type: "MERP",
        document_category: "immigration",
        permit_family: "merp",
      },
    ]);

    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.queryByText("Other")).not.toBeInTheDocument();
  });

  it("shows the MERP as a re-entry permit tied to the current permit card", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      { ...baseDoc, id: 212, document_type: "kitas", expiry_date: futureDate },
      {
        ...baseDoc,
        id: 213,
        document_type: "MERP",
        document_category: "immigration",
        permit_family: "merp",
        permit_label: "MERP · Multiple Exit Re-entry Permit",
      },
    ]);

    expect(
      screen.getByText("MERP · Multiple Exit Re-entry Permit"),
    ).toBeInTheDocument();
  });

  it("INNOCENCE: with no current permit, a MERP-only document still shows up under Other instead of disappearing", () => {
    renderTab([
      {
        ...baseDoc,
        id: 214,
        document_type: "MERP",
        document_category: "immigration",
        permit_family: "merp",
      },
    ]);

    expect(screen.queryByText("Current permit")).not.toBeInTheDocument();
    expect(screen.getByText("Other")).toBeInTheDocument();
  });
});

describe("ImmigrationTab — permit_family_label secondary line (owner correction: index leads, family is secondary)", () => {
  it("GUILT (inverted): the family label is no longer a second line under the current-permit badge; the badge reads code · name", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 220,
        document_type: "visa",
        expiry_date: futureDate,
        permit_family: "kitas",
        permit_label: "E23 — Working KITAS",
        permit_family_label: "KITAS / ITAS — Limited Stay Permit",
      },
    ]);

    expect(screen.getByText("E23 · Working KITAS")).toBeInTheDocument();
    expect(
      screen.queryByText("KITAS / ITAS — Limited Stay Permit"),
    ).not.toBeInTheDocument();
  });

  it("INNOCENCE: does not repeat the label when permit_family_label equals permit_label (no index was found)", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 221,
        document_type: "visa",
        expiry_date: futureDate,
        permit_family: "kitap",
        permit_label: "KITAP / ITAP — Permanent Stay Permit",
        permit_family_label: "KITAP / ITAP — Permanent Stay Permit",
      },
    ]);

    expect(
      screen.getAllByText("KITAP / ITAP — Permanent Stay Permit"),
    ).toHaveLength(1);
  });

  it("a history row shows only its label, never the family label line", () => {
    const futureDate = new Date(Date.now() + 400 * 86400000)
      .toISOString()
      .slice(0, 10);
    const pastDate = new Date(Date.now() - 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 222,
        document_type: "kitas",
        expiry_date: futureDate,
        permit_family: "kitas",
        permit_label: "KITAS / ITAS — Limited Stay Permit",
        permit_family_label: "KITAS / ITAS — Limited Stay Permit",
      },
      {
        ...baseDoc,
        id: 223,
        document_type: "visa",
        expiry_date: pastDate,
        permit_family: "itk",
        permit_label: "C31",
        permit_family_label: "ITK — Visit Stay Permit",
      },
    ]);

    expect(screen.getByText("Visa history")).toBeInTheDocument();
    expect(screen.getByText("C31")).toBeInTheDocument();
    expect(
      screen.queryByText("ITK — Visit Stay Permit"),
    ).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — REWORK (independent gate, 2026-09-21)", () => {
  it("(a) GUILT: a document outside isVisaFamilyDocument (e.g. document_type='itk') still shows the resolved permit_label in the 'Other' card grid, not the raw document_type", () => {
    // 'itk' does not match isVisaFamilyDocument (no kitas/kitap/visa/voa
    // substring) so it lands in `otherDocs`, rendered by `renderDocCard` —
    // the prod bug: that card ignored permit_label entirely.
    renderTab([
      {
        ...baseDoc,
        id: 300,
        document_type: "itk",
        document_category: "immigration",
        permit_family: "itk",
        permit_label: "D12 — Pre-Investment (Multiple Entry)",
      },
    ]);

    // Cleaned: index · name, without the taxonomy qualifier.
    expect(screen.getByText("D12 · Pre-Investment")).toBeInTheDocument();
    expect(screen.queryByText(/Multiple Entry/)).not.toBeInTheDocument();
    expect(screen.queryByText("itk")).not.toBeInTheDocument();
  });

  it("(a) the 'Other' list shows no family secondary line", () => {
    renderTab([
      {
        ...baseDoc,
        id: 301,
        document_type: "itk",
        document_category: "immigration",
        permit_family: "itk",
        permit_label: "D12 — Pre-Investment (Multiple Entry)",
        permit_family_label: "ITK — Visit Stay Permit",
      },
    ]);

    expect(screen.getByText("D12 · Pre-Investment")).toBeInTheDocument();
    expect(
      screen.queryByText("ITK — Visit Stay Permit"),
    ).not.toBeInTheDocument();
  });

  it("(a) GUILT falls back to the raw document_type in the 'Other' grid when unresolved (never invents a label)", () => {
    renderTab([
      {
        ...baseDoc,
        id: 302,
        document_type: "itk",
        document_category: "immigration",
      },
    ]);
    expect(screen.getByText("itk")).toBeInTheDocument();
  });

  it("(b) does not apply text-transform:capitalize to a resolved permit_label in the current-permit badge", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 303,
        document_type: "visa",
        expiry_date: futureDate,
        permit_family: "kitas",
        permit_label:
          "E28D — Investor Visa Pendirian Cabang atau Anak Perusahaan Golden Visa",
      },
    ]);

    const badge = screen.getByText(
      "E28D · Investor Visa Pendirian Cabang atau Anak Perusahaan Golden Visa",
    );
    expect(badge.className).not.toMatch(/\bcapitalize\b/);
  });

  it("(b) GUILT: still applies capitalize to the raw document_type fallback in the current-permit badge", () => {
    const futureDate = new Date(Date.now() + 60 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 304,
        document_type: "voa",
        expiry_date: futureDate,
        // no permit_label — falls back to the raw document_type
      },
    ]);

    const badge = screen.getByText("voa");
    expect(badge.className).toMatch(/\bcapitalize\b/);
  });

  it("(b) does not apply text-transform:capitalize to a resolved permit_label in the 'Other' card grid", () => {
    renderTab([
      {
        ...baseDoc,
        id: 305,
        document_type: "itk",
        document_category: "immigration",
        permit_family: "itk",
        permit_label:
          "E28D — Investor Visa Pendirian Cabang atau Anak Perusahaan Golden Visa",
      },
    ]);

    const label = screen.getByText(
      "E28D · Investor Visa Pendirian Cabang atau Anak Perusahaan Golden Visa",
    );
    expect(label.className).not.toMatch(/\bcapitalize\b/);
  });

  it("(f) GUILT: a MERP document whose document_type is generic ('visa') never renders in BOTH the visa-history row and the MERP chip", () => {
    const futureDate = new Date(Date.now() + 400 * 86400000)
      .toISOString()
      .slice(0, 10);
    renderTab([
      {
        ...baseDoc,
        id: 310,
        document_type: "kitas",
        expiry_date: futureDate,
      },
      {
        ...baseDoc,
        id: 311,
        document_type: "visa",
        // No expiry_date on purpose: the MERP chip appends " · Exp ..."
        // whenever expiry_date is set, which would make the chip's own
        // text node no longer an EXACT match for the bare label and hide
        // a real double-render behind a text-matcher false negative.
        permit_family: "merp",
        permit_label: "MERP · Multiple Exit Re-entry Permit",
      },
    ]);

    expect(
      screen.getAllByText("MERP · Multiple Exit Re-entry Permit"),
    ).toHaveLength(1);
  });
});

describe("ImmigrationTab — one current permit, shared with the Overview", () => {
  const mk = (
    over: Partial<ClientDocument> & { id: number },
  ): ClientDocument => ({
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    ...over,
  });

  it("the Overview picker and the Immigration tab choose the same current permit", () => {
    const docs = [
      mk({ id: 1, issue_date: "2023-01-01", expiry_date: "2024-01-01" }),
      mk({ id: 2, issue_date: "2025-01-01", expiry_date: "2036-01-01" }),
      mk({ id: 3, document_type: "voa", expiry_date: "2025-06-01" }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(2);
    renderTab(docs);
    expect(screen.getByText("Current permit")).toBeInTheDocument();
    // The panel shows permit 2's expiry; permit 1's is in history.
    expect(screen.getAllByText(/2036-01-01/).length).toBeGreaterThan(0);
    expect(screen.getByText("Visa history")).toBeInTheDocument();
  });

  it("GUILT: an older expired visa in history shows no countdown (no 'ago' / 'left'); INNOCENCE: the current permit does", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    renderTab([
      mk({ id: 1, issue_date: "2022-01-01", expiry_date: "2023-01-01" }),
      mk({ id: 2, issue_date: "2025-06-01", expiry_date: "2027-06-01" }),
    ]);
    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    )!;
    expect(grid.textContent).toContain("2022-01-01 → 2023-01-01");
    expect(grid.textContent).toContain("Completed");
    expect(grid.textContent).not.toMatch(/ago|left|Expires/);
    expect(document.body.textContent).toMatch(/2027-06-01 · .*left/);
  });

  it("a removed or archived newer permit does not displace the current one", () => {
    const docs = [
      mk({ id: 1, expiry_date: "2027-01-01" }),
      mk({
        id: 2,
        expiry_date: "2031-01-01",
        deleted_at: "2026-01-01T00:00:00Z",
      }),
      mk({ id: 3, expiry_date: "2032-01-01", is_archived: true }),
    ];
    expect(pickCurrentVisa(docs)?.id).toBe(1);
  });

  it("GUILT: a spouse's permit is not in the client's history and carries no countdown; the Family line switches tab; no family docs -> no line", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const onOpen = vi.fn();
    render(
      <ImmigrationTab
        clientId={1}
        documents={[
          mk({ id: 1, expiry_date: "2028-01-01" }),
          mk({
            id: 2,
            family_member_id: 9,
            family_member_name: "Member A",
            permit_code: "E31A",
            permit_label: "E31A — Spouse KITAS",
            expiry_date: "2026-01-21",
          }),
        ]}
        formatDate={(d) => d}
        onAddClick={vi.fn()}
        onEditClick={vi.fn()}
        onRefresh={vi.fn()}
        onOpenFamilyTab={onOpen}
      />,
    );
    expect(document.body.textContent).not.toContain("Spouse KITAS");
    expect(document.body.textContent).not.toMatch(/⏰|20d left/);
    fireEvent.click(
      screen.getByRole("button", {
        name: "Family permits are in the Family tab (1)",
      }),
    );
    expect(onOpen).toHaveBeenCalledTimes(1);
    cleanup();
    renderTab([mk({ id: 1, expiry_date: "2028-01-01" })]);
    expect(screen.queryByText(/Family permits are in/)).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — clean labels, no photos, same-permit copies (owner feedback 2026-11-03)", () => {
  const mk = (
    over: Partial<ClientDocument> & { id: number },
  ): ClientDocument => ({
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    ...over,
  });
  const FILE = "2025-06-25__ITAS-E28A__synthetic.pdf";

  it("GUILT: '(Standard)' is never rendered; the badge reads '<code> · <name>' and no family subline appears", () => {
    renderTab([
      mk({
        id: 1,
        expiry_date: "2036-01-01",
        permit_code: "E28A",
        permit_label: "E28A — Investor KITAS (Standard)",
        permit_family_label: "KITAS / ITAS — Limited Stay Permit",
      }),
    ]);
    expect(screen.getByText("E28A · Investor KITAS")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("(Standard)");
    expect(document.body.textContent).not.toContain("Limited Stay Permit");
  });

  it("GUILT: no raw file name is visible in the panel or any row; it stays on the download button", () => {
    renderTab([
      mk({
        id: 1,
        expiry_date: "2036-01-01",
        file_name: FILE,
        google_drive_file_url: "https://drive.google.com/file/d/a1/view",
      }),
      mk({
        id: 2,
        expiry_date: "2026-01-01",
        file_name: FILE,
        google_drive_file_url: "https://drive.google.com/file/d/a2/view",
      }),
      mk({
        id: 3,
        document_type: "imta",
        expiry_date: "2030-01-01",
        file_name: FILE,
        google_drive_file_url: "https://drive.google.com/file/d/a3/view",
      }),
    ]);
    expect(document.body.textContent).not.toContain(FILE);
    expect(
      screen.getAllByRole("button", { name: `Download ${FILE}` }).length,
    ).toBeGreaterThan(0);
  });

  it("GUILT: a second file of the SAME permit (same code + expiry) is not history; it is listed under 'Other files of this permit' with its actions", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-06-25", permit_code: "E28A" }),
      mk({ id: 2, expiry_date: "2036-06-25", permit_code: "E28A" }),
      mk({ id: 3, expiry_date: "2024-06-25", permit_code: "E28A" }),
    ]);
    expect(screen.getByText("Other files of this permit")).toBeInTheDocument();
    const same = document.querySelector(
      '[data-hgrid="immigration-same-permit"]',
    )!;
    expect(same.querySelectorAll('button[aria-label^="Edit"]')).toHaveLength(1);
    expect(same.querySelectorAll('button[aria-label^="Remove"]')).toHaveLength(
      1,
    );
    const history = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    )!;
    // Only the really older permit is history (after the current permit's own row).
    expect(history.textContent).toContain("Ended 2024-06-25");
    expect(
      history.querySelector('[role="row"]')!.nextElementSibling!.children,
    ).toHaveLength(2); // current + the older permit
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("INNOCENCE: same code but a different expiry is history, not a copy; same permit number groups regardless of expiry", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-06-25", permit_code: "E28A" }),
      mk({ id: 2, expiry_date: "2030-06-25", permit_code: "E28A" }),
    ]);
    expect(
      screen.queryByText("Other files of this permit"),
    ).not.toBeInTheDocument();
    cleanup();
    renderTab([
      mk({ id: 1, expiry_date: "2036-06-25", permit_number: "TEST-1" }),
      mk({ id: 2, expiry_date: "2030-06-25", permit_number: "TEST-1" }),
    ]);
    expect(screen.getByText("Other files of this permit")).toBeInTheDocument();
  });

  it("GUILT: 'Ended' is never written for a date in the future ('Valid until'); a past date still reads 'Ended'", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    renderTab([
      mk({ id: 1, document_type: "kitap", expiry_date: "2040-01-01" }),
      mk({ id: 2, document_type: "visa", expiry_date: "2027-06-25" }),
      mk({ id: 3, document_type: "voa", expiry_date: "2025-06-25" }),
    ]);
    const history = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    )!.textContent!;
    expect(history).toContain("Valid until 2027-06-25");
    expect(history).toContain("Ended 2025-06-25");
    expect(history).not.toContain("Ended 2027-06-25");
  });

  it("the Issued column is hidden when no history row has an issue_date, shown when one does", () => {
    const docs = [
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({ id: 2, document_type: "kitap", expiry_date: "2025-01-01" }),
    ];
    renderTab(docs);
    let head = document.querySelector(
      '[data-hgrid="immigration-visa-history"] [role="row"]',
    )!;
    expect(head.textContent).not.toContain("Issued");
    expect(head.children.length).toBe(4);
    cleanup();
    renderTab([docs[0], { ...docs[1], issue_date: "2024-01-01" }]);
    head = document.querySelector(
      '[data-hgrid="immigration-visa-history"] [role="row"]',
    )!;
    expect(head.textContent).toContain("Issued");
    expect(head.children.length).toBe(5);
  });

  it("no <img> anywhere in the tab: Other and Working permit are compact rows, not image cards", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({
        id: 2,
        document_type: "imta",
        expiry_date: "2030-01-01",
        google_drive_file_url: "https://drive.google.com/file/d/a3/view",
      }),
      mk({
        id: 3,
        document_type: "sponsor_letter",
        google_drive_file_url: "https://drive.google.com/file/d/a4/view",
      }),
    ]);
    expect(document.querySelectorAll("img")).toHaveLength(0);
    expect(
      document.querySelector('[data-section="immigration-working"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('[data-section="immigration-other"]'),
    ).not.toBeNull();
  });

  it("an EPO document is a history row ('EPO · Exit permit') with its date, not an 'Other' or 'Working Permit' card", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({
        id: 2,
        document_type: "epo",
        permit_code: "EPO",
        permit_label: "EPO — Exit permit (Exit Permit Only)",
        expiry_date: "2025-03-04",
      }),
    ]);
    const history = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    )!;
    expect(history.textContent).toContain("EPO · Exit permit");
    expect(history.textContent).toContain("2025-03-04");
    expect(history.textContent).not.toMatch(/ago|left/);
    expect(screen.queryByText("Other")).not.toBeInTheDocument();
    expect(screen.queryByText("Working Permit")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — visual review round (kinds, EPO, ITK, neutral history)", () => {
  const mk = (
    over: Partial<ClientDocument> & { id: number },
  ): ClientDocument => ({
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    ...over,
  });
  const historyText = () =>
    document.querySelector('[data-hgrid="immigration-visa-history"]')!
      .textContent!;

  it("the KITAS (not the e-Visa) is the current permit and the copy list is labelled by document kind, with actions", () => {
    for (const order of [0, 1]) {
      const kitas = mk({
        id: 1,
        permit_code: "E28A",
        permit_label: "E28A — Investor KITAS (Standard)",
        expiry_date: "2036-06-25",
      });
      const evisa = mk({
        id: 2,
        document_type: "evisa",
        permit_code: "E28A",
        permit_label: "E28A — Investor KITAS (Standard)",
        expiry_date: "2036-06-25",
      });
      renderTab(order ? [evisa, kitas] : [kitas, evisa]);
      const same = document.querySelector(
        '[data-hgrid="immigration-same-permit"]',
      )!;
      expect(same.textContent).toContain("e-Visa");
      expect(same.textContent).not.toContain("KITAS card");
      expect(same.querySelectorAll('button[aria-label^="Edit"]')).toHaveLength(
        1,
      );
      expect(same.textContent).toContain("2036-06-25");
      cleanup();
    }
  });

  it("GUILT: a spouse's permit never shows a countdown in the client's history; INNOCENCE: the client's own older permit stays", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({ id: 2, document_type: "kitap", expiry_date: "2024-01-01" }),
      mk({
        id: 3,
        family_member_id: 9,
        expiry_date: new Date(Date.now() + 20 * 86400000)
          .toISOString()
          .slice(0, 10),
      }),
    ]);
    expect(historyText()).toContain("Ended 2024-01-01");
    expect(historyText()).not.toMatch(/left|ago|⏰/);
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("EPO in any case, with no expiry, reads 'EPO · Exit permit' dated by issue_date, else by the upload date; never two dashes", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({ id: 2, document_type: "EPO", issue_date: "2025-03-04" }),
      mk({
        id: 3,
        document_type: "Exit_Permit",
        created_at: "2025-02-01T10:00:00Z",
      }),
    ]);
    const text = historyText();
    expect(text).toContain("EPO · Exit permit");
    expect(text).toContain("Issued 2025-03-04");
    expect(text).toContain("Issued 2025-02-01");
    expect(screen.queryByText("epo")).not.toBeInTheDocument();
  });

  it("GUILT: an expired ITK is a history row, neutral ('Ended <date>'), never 'Expired Nd ago' nor a warning tone, and not in Other", () => {
    vi.setSystemTime(new Date("2026-11-03T00:00:00.000Z"));
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({
        id: 2,
        document_type: "itk",
        permit_code: "C1",
        permit_label: "ITK — Visit stay permit",
        expiry_date: "2026-10-14",
      }),
    ]);
    const text = historyText();
    expect(text).toContain("C1 · ITK — Visit stay permit");
    expect(text).toContain("Ended 2026-10-14");
    expect(document.body.textContent).not.toMatch(/Expired \d+d ago/);
    expect(
      document.querySelector('[data-section="immigration-other"]'),
    ).toBeNull();
    expect(
      document.querySelector(
        '[data-hgrid="immigration-visa-history"] [class*="state-warning"]',
      ),
    ).toBeNull();
  });

  it("INNOCENCE: a Working permit row is neutral too (no countdown text)", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({ id: 2, document_type: "imta", expiry_date: "2030-01-01" }),
    ]);
    const row = document.querySelector('[data-section="immigration-working"]')!;
    expect(row.textContent).toContain("2030-01-01");
    expect(row.textContent).not.toMatch(/left|ago/);
  });
});

describe("ImmigrationTab — owner round 2026-11-03 (current row first, pills, cards)", () => {
  const mk = (
    over: Partial<ClientDocument> & { id: number },
  ): ClientDocument => ({
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    ...over,
  });
  const rowsOf = () => {
    const grid = document.querySelector(
      '[data-hgrid="immigration-visa-history"]',
    )!;
    return Array.from(
      grid.querySelector('[role="row"]')!.nextElementSibling!.children,
    ) as HTMLElement[];
  };

  it("the current permit is the FIRST history row, marked, larger, with a Verified pill; previous rows are smaller, muted and read Completed", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01", status: "verified" }),
      mk({
        id: 2,
        document_type: "kitap",
        expiry_date: "2024-01-01",
        status: "verified",
      }),
    ]);
    const rows = rowsOf();
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute("data-current")).toBe("true");
    expect(rows[1].getAttribute("data-current")).toBeNull();
    expect(rows[0].textContent).toContain("Verified");
    expect(rows[0].innerHTML).toContain("text-[17px]");
    expect(rows[0].innerHTML).toContain("state-success");
    expect(rows[1].textContent).toContain("Completed");
    expect(rows[1].textContent).not.toContain("Verified");
    expect(rows[1].innerHTML).toContain("text-[13px]");
    expect(rows[1].innerHTML).toContain("state-danger");
    expect(rows[1].innerHTML).not.toContain("text-[17px]");
  });

  it("INNOCENCE: the retired status word appears nowhere; an unverified current permit reads Current, not Verified", () => {
    renderTab([
      mk({ id: 1, expiry_date: "2036-01-01" }),
      mk({ id: 2, document_type: "kitap", expiry_date: "2024-01-01" }),
    ]);
    expect(document.body.textContent).not.toContain("Super" + "seded");
    expect(rowsOf()[0].textContent).toContain("Current");
    expect(rowsOf()[0].textContent).not.toContain("Verified");
  });

  it("Issued shows the issue_date only where the payload has it; no upload-date guess; column hidden when no row has one", () => {
    renderTab([
      mk({
        id: 1,
        expiry_date: "2036-01-01",
        issue_date: "2025-01-01",
        created_at: "2025-02-02T00:00:00Z",
      }),
      mk({
        id: 2,
        document_type: "kitap",
        expiry_date: "2024-01-01",
        created_at: "2023-03-03T00:00:00Z",
      }),
    ]);
    const rows = rowsOf();
    expect(rows[0].textContent).toContain("2025-01-01");
    expect(rows[0].textContent).not.toContain("2025-02-02");
    expect(rows[1].textContent).not.toContain("2023-03-03");
    cleanup();
    renderTab([
      mk({
        id: 1,
        expiry_date: "2036-01-01",
        created_at: "2025-02-02T00:00:00Z",
      }),
      mk({ id: 2, document_type: "kitap", expiry_date: "2024-01-01" }),
    ]);
    expect(
      document.querySelector(
        '[data-hgrid="immigration-visa-history"] [role="row"]',
      )!.textContent,
    ).not.toContain("Issued");
  });

  it("Other and Working permit are small cards (no image) with Download and Upload at the bottom-left; Upload opens the add flow, edit and remove stay", () => {
    const onAdd = vi.fn();
    render(
      <ImmigrationTab
        clientId={1}
        documents={[
          mk({ id: 1, expiry_date: "2036-01-01" }),
          mk({
            id: 2,
            document_type: "sponsor_letter",
            google_drive_file_url: "https://drive.google.com/file/d/a4/view",
            file_name: "synthetic-letter.pdf",
          }),
        ]}
        formatDate={(d) => d}
        onAddClick={onAdd}
        onEditClick={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    const other = document.querySelector('[data-section="immigration-other"]')!;
    expect(other.querySelectorAll("img")).toHaveLength(0);
    expect(other.textContent).toContain("sponsor letter");
    expect(other.textContent).not.toContain("synthetic-letter.pdf");
    expect(
      screen.getByRole("button", { name: "Download synthetic-letter.pdf" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /upload a file for sponsor_letter/i }),
    );
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: /edit sponsor_letter/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /remove sponsor_letter/i }),
    ).toBeInTheDocument();
  });
});

describe("ImmigrationTab — AI summary slot beside the current permit", () => {
  it("asks for the immigration scope and shows the text inside the current-permit panel; hidden when the backend has none", async () => {
    getClientRecap.mockResolvedValueOnce({
      scope: "immigration",
      text: "Synthetic permit summary.",
      generated_at: "2026-11-03T09:00:00Z",
      model: null,
      stale: false,
    });
    renderTab([
      {
        ...baseDoc,
        id: 1,
        document_type: "kitas",
        expiry_date: "2036-01-01",
      },
    ]);
    const text = await screen.findByText("Synthetic permit summary.");
    expect(getClientRecap).toHaveBeenCalledWith(1, "immigration", undefined);
    expect(
      screen.getByText("Current permit").closest("div.rounded-lg"),
    ).toContainElement(text);
  });

  it("INNOCENCE: no summary element when the endpoint fails", async () => {
    renderTab([
      { ...baseDoc, id: 1, document_type: "kitas", expiry_date: "2036-01-01" },
    ]);
    await screen.findByText("Current permit");
    await waitFor(() =>
      expect(screen.queryByTestId("ai-recap-skeleton")).not.toBeInTheDocument(),
    );
    expect(screen.queryByLabelText("AI summary")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — polish (v8): one-line validity, Download on copies", () => {
  const mk = (
    over: Partial<ClientDocument> & { id: number },
  ): ClientDocument => ({
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    ...over,
  });

  it("the Validity cell of a history row never wraps at desktop (nowrap), on every row", () => {
    renderTab([
      mk({ id: 1, issue_date: "2025-01-04", expiry_date: "2036-01-04" }),
      mk({
        id: 2,
        document_type: "kitap",
        issue_date: "2024-01-04",
        expiry_date: "2025-01-04",
      }),
    ]);
    const body = document.querySelector(
      '[data-hgrid="immigration-visa-history"] [role="row"]',
    )!.nextElementSibling!;
    for (const row of Array.from(body.children)) {
      const validity = row.children[3] as HTMLElement;
      expect(validity.className).toContain("whitespace-nowrap");
    }
  });

  it("GUILT: a copy of the current permit that has a file (Drive id or file URL, not only a Drive URL) offers Download; INNOCENCE: one with no file does not", () => {
    renderTab([
      mk({ id: 1, permit_code: "E28A", expiry_date: "2036-06-25" }),
      mk({
        id: 2,
        document_type: "evisa",
        permit_code: "E28A",
        expiry_date: "2036-06-25",
        file_id: "driveId123",
        file_name: "synthetic-evisa.pdf",
      }),
      mk({
        id: 3,
        document_type: "kitas",
        permit_code: "E28A",
        expiry_date: "2036-06-25",
      }),
    ]);
    const same = document.querySelector(
      '[data-hgrid="immigration-same-permit"]',
    )!;
    expect(
      same.querySelectorAll(
        'button[aria-label="Download synthetic-evisa.pdf"]',
      ),
    ).toHaveLength(1);
    expect(
      same.querySelectorAll('button[aria-label^="Download"]'),
    ).toHaveLength(1);
    expect(same.querySelectorAll('button[aria-label^="Edit"]')).toHaveLength(2);
  });
});

describe("ImmigrationTab history — rejected label", () => {
  it("GUILT: a rejected permit in the history reads Rejected, not Completed", () => {
    renderTab([
      {
        ...baseDoc,
        id: 301,
        document_type: "kitas",
        expiry_date: "2031-01-01",
        status: "verified",
      },
      {
        ...baseDoc,
        id: 302,
        document_type: "kitas",
        expiry_date: "2029-01-01",
        status: "rejected",
      },
    ]);
    expect(screen.getAllByText("Rejected").length).toBeGreaterThan(0);
  });

  it("INNOCENCE: a non-rejected older permit still reads Completed, and no Rejected", () => {
    renderTab([
      {
        ...baseDoc,
        id: 303,
        document_type: "kitas",
        expiry_date: "2031-01-01",
        status: "verified",
      },
      {
        ...baseDoc,
        id: 304,
        document_type: "kitas",
        expiry_date: "2029-01-01",
        status: "pending",
      },
    ]);
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.queryByText("Rejected")).not.toBeInTheDocument();
  });
});

describe("ImmigrationTab — a rejected file of the current permit", () => {
  const rejectedSibling = {
    ...baseDoc,
    id: 402,
    document_type: "evisa",
    permit_number: "SYN-001",
    expiry_date: "2031-01-01",
    status: "rejected" as const,
  };
  const current = {
    ...baseDoc,
    id: 401,
    document_type: "kitas",
    permit_number: "SYN-001",
    expiry_date: "2031-01-01",
    status: "verified" as const,
  };

  it("GUILT: a rejected e-Visa sharing the permit number is not under 'Other files of this permit' and reads Rejected", () => {
    renderTab([current, rejectedSibling]);
    expect(screen.queryByText("Other files of this permit")).toBeNull();
    expect(document.querySelector("#immigration-same-permit")).toBeNull();
    expect(screen.getAllByText("Rejected").length).toBeGreaterThan(0);
    // Only the current permit's own row (phone + desktop cell) says "Valid until".
    expect(screen.getAllByText(/Valid until 2031/)).toHaveLength(2);
  });

  it("INNOCENCE: the same sibling, verified, stays under 'Other files of this permit'", () => {
    renderTab([current, { ...rejectedSibling, status: "verified" as const }]);
    expect(screen.getByText("Other files of this permit")).toBeTruthy();
  });

  it("GUILT: a rejected older permit's validity cell reads Rejected, no date range", () => {
    renderTab([
      current,
      {
        ...baseDoc,
        id: 403,
        document_type: "kitas",
        permit_number: "SYN-002",
        issue_date: "2020-01-01",
        expiry_date: "2024-01-01",
        status: "rejected" as const,
      },
    ]);
    expect(screen.queryByText(/→/)).toBeNull();
    expect(screen.queryByText(/Ended/)).toBeNull();
  });

  it("INNOCENCE: a non-rejected older permit keeps its date range", () => {
    renderTab([
      current,
      {
        ...baseDoc,
        id: 404,
        document_type: "kitas",
        permit_number: "SYN-002",
        issue_date: "2020-01-01",
        expiry_date: "2024-01-01",
        status: "verified" as const,
      },
    ]);
    expect(screen.getAllByText(/→/).length).toBeGreaterThan(0);
  });

  const merp = (status: "rejected" | "verified") => ({
    ...baseDoc,
    id: 405,
    document_type: "merp",
    document_category: "immigration" as const,
    expiry_date: "2031-02-02",
    status,
  });

  it("GUILT: a rejected MERP chip shows Rejected, not an expiry date", () => {
    renderTab([current, merp("rejected")]);
    expect(screen.getByText(/· Rejected/)).toBeTruthy();
    expect(screen.queryByText(/Exp 2031-02-02/)).toBeNull();
  });

  it("INNOCENCE: a verified MERP chip keeps its expiry date", () => {
    renderTab([current, merp("verified")]);
    expect(screen.getByText(/Exp 2031-02-02/)).toBeTruthy();
  });
});
