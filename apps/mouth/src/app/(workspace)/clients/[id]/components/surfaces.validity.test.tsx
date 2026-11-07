/**
 * The validity model on every surface (DOCUMENT_VALIDITY_SPEC.md): a void
 * document (rejected, removed, archived) reads as not current, not filed,
 * not renewable and not counted; its live twin with the same fields still
 * counts and appears. Synthetic fixtures only.
 */
import {
  cleanup as cleanupAll,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ClientDocument,
  ClientProfile,
  FamilyMember,
} from "@/lib/api/crm/crm.types";
import { DocumentsTab } from "./DocumentsTab";
import { ImmigrationTab, isRenewable } from "./ImmigrationTab";
import { FamilyTab } from "./FamilyTab";
import { OverviewTab } from "./OverviewTab";
import { PassportCard } from "./PassportCard";
import { ProcessTab } from "./ProcessTab";
import { ValidityTrack } from "./ValidityTrack";
import { VisaCard } from "./VisaCard";
import { formatCardDate } from "./documentValidity";
import { BROWSER_ZONES, asBrowserIn } from "./browserTimeZone.testutil";
import { buildLedger } from "./attention";
import { dayToInstant } from "./witaClock";

vi.mock("@/lib/api", () => ({
  api: {
    post: vi.fn(),
    request: vi.fn(),
    crm: {
      deleteDocument: vi.fn(),
      deleteFamilyMember: vi.fn(),
      deletePractice: vi.fn(),
      getClientRecap: vi.fn().mockRejectedValue(new Error("no recap")),
      extractPassportForClient: vi.fn(),
    },
  },
}));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("./AiSummaryCard", () => ({ AiSummaryCard: () => null }));
vi.mock("./VaultCard", () => ({ VaultCard: () => null }));
vi.mock("./WaCaseIntelligencePanel", () => ({
  WaCaseIntelligencePanel: () => null,
}));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

const DAY = "2026-11-07";

/** A date `n` days from DAY (2026-11-07). */
const plus = (n: number): string => {
  const base = Date.UTC(2026, 10, 7) + n * 86_400_000;
  return new Date(base).toISOString().slice(0, 10);
};

const fmt = (d: string) => `f:${d}`;

const doc = (
  over: Partial<ClientDocument> & { id: number },
): ClientDocument => ({
  client_id: 1,
  document_type: "kitas",
  document_category: "immigration",
  ...over,
});

afterEach(() => vi.useRealTimers());

/** The data rows of a Hairline grid (the header row is not one). */
const gridRows = () =>
  Array.from(document.querySelectorAll<HTMLElement>('[class*="group/row"]'));

// ---------------------------------------------------------------------------
// Documents tab (rows 20-24)
// ---------------------------------------------------------------------------

const renderDocs = (
  rows: ClientDocument[],
  byCategory: Record<string, ClientDocument[]> = {
    other: rows.filter((r) => !r.deleted_at && r.status !== "rejected"),
  },
  extra: { initialGroup?: string; day?: string } = {},
) =>
  render(
    <DocumentsTab
      clientId={1}
      documents={rows}
      documentsByCategory={byCategory}
      formatDate={fmt}
      onAddClick={vi.fn()}
      onEditClick={vi.fn()}
      onRefresh={vi.fn()}
      day={extra.day ?? DAY}
      initialGroup={extra.initialGroup}
    />,
  );

const generalDoc = (over: Partial<ClientDocument> & { id: number }) =>
  doc({ document_type: "bank_statement", document_category: "other", ...over });

describe("Documents tab — pill, cell and holding count (rows 20-22)", () => {
  it.each([
    ["rejected", { status: "rejected" }, "Rejected"],
    [
      "removed by the client",
      { deleted_at: "2026-10-01T10:00:00Z" },
      "Removed",
    ],
    ["archived", { is_archived: true }, "Archived"],
    ["deleted status", { status: "deleted" }, "Removed"],
  ] as const)(
    "GUILT: a %s document reads its reason and never a positive word",
    (_n, fields, word) => {
      renderDocs([
        generalDoc({
          id: 1,
          status: "verified",
          expiry_date: plus(200),
          ...fields,
        } as never),
      ]);
      expect(screen.getAllByText(word).length).toBeGreaterThan(0);
      expect(screen.queryByText("Valid")).not.toBeInTheDocument();
      expect(screen.queryByText("Verified")).not.toBeInTheDocument();
    },
  );

  it("verified reads Valid in valid and expiring, Expired when expired, Verified when undated", () => {
    renderDocs(
      [
        generalDoc({ id: 1, status: "verified", expiry_date: plus(200) }),
        generalDoc({ id: 2, status: "verified", expiry_date: plus(10) }),
        generalDoc({ id: 3, status: "verified", expiry_date: plus(0) }),
        generalDoc({ id: 4, status: "verified" }),
      ],
      { other: [] },
    );
    const rows = gridRows();
    const pillOf = (n: number) =>
      within(rows[n]).getAllByText(/^(Valid|Expired|Verified)$/)[0].textContent;
    // rows are ordered by nearest expiry first: 0, 10, 200, undated
    expect(rows.map((_r, i) => pillOf(i))).toEqual([
      "Expired",
      "Valid",
      "Valid",
      "Verified",
    ]);
  });

  it("a void document shows its date and no countdown, no warning colour", () => {
    const { container } = renderDocs([
      generalDoc({ id: 1, status: "rejected", expiry_date: plus(10) }),
    ]);
    expect(container.textContent).toContain(`f:${plus(10)}`);
    expect(screen.queryByText(/left/)).not.toBeInTheDocument();
    expect(container.innerHTML).not.toContain("--state-warning");
  });

  it("GUILT: rejected and removed documents are not in the 'N expiring soon' total", () => {
    renderDocs([
      generalDoc({ id: 1, status: "rejected", expiry_date: plus(10) }),
      generalDoc({
        id: 2,
        deleted_at: "2026-10-01T10:00:00Z",
        expiry_date: plus(20),
      }),
      generalDoc({ id: 3, is_archived: true, expiry_date: plus(30) }),
    ]);
    expect(screen.queryByText(/expiring soon/)).not.toBeInTheDocument();
  });

  it("INNOCENCE: the live twin with the same fields IS counted", () => {
    renderDocs([
      generalDoc({ id: 1, status: "rejected", expiry_date: plus(10) }),
      generalDoc({ id: 2, status: "received", expiry_date: plus(10) }),
    ]);
    expect(screen.getByText("1 expiring soon")).toBeInTheDocument();
  });

  it("the 90-day window: day 90 counts, day 91 does not; an undated live document never counts", () => {
    renderDocs([
      generalDoc({ id: 1, expiry_date: plus(90) }),
      generalDoc({ id: 2, expiry_date: plus(91) }),
      generalDoc({ id: 3 }),
    ]);
    expect(screen.getByText("1 expiring soon")).toBeInTheDocument();
  });

  it("day 0 reads 'Expired today' and counts as expiring soon", () => {
    renderDocs([generalDoc({ id: 1, expiry_date: DAY })]);
    expect(screen.getAllByText("Expired today").length).toBeGreaterThan(0);
    expect(screen.getByText("1 expiring soon")).toBeInTheDocument();
  });

  it("a status of expired with a future date is not shown as expired", () => {
    renderDocs([
      generalDoc({ id: 1, status: "expired", expiry_date: plus(200) }),
    ]);
    expect(screen.queryByText(/Expired/)).not.toBeInTheDocument();
  });

  it("a live document with a garbage date shows a dash, no countdown, never counted", () => {
    renderDocs([generalDoc({ id: 1, expiry_date: "next spring" })]);
    expect(screen.queryByText(/left|Expired/)).not.toBeInTheDocument();
    expect(screen.queryByText(/expiring soon/)).not.toBeInTheDocument();
  });
});

describe("Documents tab — one 'Removed or rejected' group (rows 23-24)", () => {
  const rows = () => [
    generalDoc({ id: 1, file_name: "live-a.pdf" }),
    generalDoc({ id: 2, file_name: "live-b.pdf", expiry_date: plus(10) }),
    generalDoc({ id: 3, file_name: "rejected.pdf", status: "rejected" }),
    generalDoc({
      id: 4,
      file_name: "removed.pdf",
      deleted_at: "2026-10-01T10:00:00Z",
    }),
    doc({
      id: 5,
      document_type: "passport",
      document_category: "personal",
      file_name: "void-passport.pdf",
      status: "rejected",
    }),
  ];
  const live = () => rows().filter((r) => r.id <= 2);

  it("lists every void document of any kind in the group, once", () => {
    renderDocs(rows(), { other: live() }, { initialGroup: "removed" });
    expect(gridRows()).toHaveLength(3);
    for (const name of ["rejected.pdf", "removed.pdf", "void-passport.pdf"]) {
      expect(screen.getAllByText(name)).toHaveLength(1);
    }
    expect(screen.queryByText("live-a.pdf")).not.toBeInTheDocument();
  });

  it("the group pill and the listing counts equal the rows they label", () => {
    renderDocs(rows(), { other: live() });
    expect(screen.getByLabelText("Documents")).toHaveTextContent("5");
    expect(
      screen.getByRole("button", { name: /^All · 5/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Removed or rejected · 3" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Removed or rejected · 3" }),
    );
    expect(screen.getByLabelText("Documents")).toHaveTextContent("3");
  });

  it("a category pill counts live rows only, and a void general document is not in it", () => {
    renderDocs(rows(), { other: live() });
    fireEvent.click(screen.getByRole("button", { name: /^Other · 2/ }));
    expect(gridRows()).toHaveLength(2);
    expect(screen.queryByText("rejected.pdf")).not.toBeInTheDocument();
  });

  it("the group does not exist when nothing is void", () => {
    renderDocs(live(), { other: live() });
    expect(
      screen.queryByRole("button", { name: /Removed or rejected/ }),
    ).not.toBeInTheDocument();
  });

  it("the empty state shows only when the tab has no rows at all — void rows keep the tab", () => {
    renderDocs([generalDoc({ id: 9, status: "rejected" })], { other: [] });
    expect(screen.queryByText("No documents on file yet.")).toBeNull();
    renderDocs([], {});
    expect(screen.getByText("No documents on file yet.")).toBeInTheDocument();
  });

  it("the Documents tab follows the day it is given, not the clock", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-12-31T00:00:00Z"));
    renderDocs([generalDoc({ id: 1, expiry_date: "2026-11-10" })]);
    expect(screen.getByText("⏰ 3d left")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Immigration tab (rows 10-19)
// ---------------------------------------------------------------------------

const renderImm = (
  documents: ClientDocument[],
  extra: { onOpenRemoved?: () => void; onOpenFamilyTab?: () => void } = {},
) =>
  render(
    <ImmigrationTab
      clientId={1}
      documents={documents}
      formatDate={fmt}
      onAddClick={vi.fn()}
      onEditClick={vi.fn()}
      onRefresh={vi.fn()}
      onOpenFamilyTab={extra.onOpenFamilyTab}
      onOpenRemoved={extra.onOpenRemoved}
      day={DAY}
    />,
  );

describe("Immigration tab — liveness, numerals, Start Renewal", () => {
  const live = (over: Partial<ClientDocument> = {}) =>
    doc({ id: 2, expiry_date: plus(30), issue_date: plus(-300), ...over });

  it.each([
    ["rejected", { status: "rejected" }],
    ["removed", { deleted_at: "2026-10-01T10:00:00Z" }],
    ["archived", { is_archived: true }],
  ] as const)(
    "GUILT: a %s permit with the farthest expiry is not current, not in history, not renewable; the signpost names it",
    (_n, fields) => {
      const onOpen = vi.fn();
      renderImm(
        [
          doc({ id: 1, expiry_date: plus(400), ...fields } as never),
          live({ expiry_date: plus(30) }),
        ],
        { onOpenRemoved: onOpen },
      );
      // the live twin is the current permit; nothing else is listed
      expect(screen.getByText("Current permit")).toBeInTheDocument();
      expect(screen.queryByText("Visa history")).not.toBeInTheDocument();
      expect(screen.getByText(/^Expires/)).toBeInTheDocument();
      expect(screen.queryByText(new RegExp(plus(400)))).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", {
          name: "1 removed or rejected file is in Documents",
        }),
      );
      expect(onOpen).toHaveBeenCalledOnce();
    },
  );

  it("INNOCENCE: the same permit, live, is current with no signpost", () => {
    renderImm([doc({ id: 1, expiry_date: plus(400) })]);
    expect(screen.getByText("Current permit")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(plus(400)))).toBeInTheDocument();
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });

  it("GUILT: only a void permit — the empty state, plus the signpost", () => {
    renderImm([doc({ id: 1, status: "rejected", expiry_date: plus(30) })]);
    expect(
      screen.getByText("No immigration documents yet"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Current permit")).not.toBeInTheDocument();
    expect(
      screen.getByText("1 removed or rejected file is in Documents"),
    ).toBeInTheDocument();
  });

  it("a void family permit is counted in neither the family line nor the Immigration signpost", () => {
    renderImm(
      [
        live(),
        doc({ id: 7, family_member_id: 5, status: "rejected" }),
        doc({ id: 8, family_member_id: 5, expiry_date: plus(10) }),
      ],
      { onOpenFamilyTab: vi.fn() },
    );
    expect(
      screen.getByText("Family permits are in the Family tab (1)"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });

  describe("Start Renewal (row 13)", () => {
    const renewable = (daysLeft: number | null, over = {}) =>
      isRenewable(
        doc({
          id: 1,
          expiry_date: daysLeft === null ? undefined : plus(daysLeft),
          ...over,
        }),
        DAY,
      );

    it.each([
      [-1, true],
      [0, true],
      [90, true],
      [91, false],
    ])("daysLeft %i -> %s", (n, expected) => {
      expect(renewable(n)).toBe(expected);
    });

    it("an undated live permit is never renewable", () => {
      expect(renewable(null)).toBe(false);
    });

    it.each([
      ["rejected", { status: "rejected" }],
      ["removed", { deleted_at: "2026-10-01T10:00:00Z" }],
      ["archived", { is_archived: true }],
    ])(
      "GUILT: a %s permit inside the window is not renewable",
      (_n, fields) => {
        expect(renewable(10, fields)).toBe(false);
      },
    );

    it("only the renewable types qualify", () => {
      expect(renewable(10, { document_type: "imta" })).toBe(false);
      expect(renewable(10, { document_type: "visa" })).toBe(true);
      expect(renewable(10, { document_type: "kitap" })).toBe(true);
    });

    it("the Start Renewal button shows for a live permit at day 90 and not at 91", () => {
      const first = renderImm([live({ expiry_date: plus(90) })]);
      expect(
        screen.getByRole("button", { name: "Start Renewal" }),
      ).toBeInTheDocument();
      first.unmount();
      renderImm([live({ expiry_date: plus(91) })]);
      expect(
        screen.queryByRole("button", { name: "Start Renewal" }),
      ).toBeNull();
    });
  });

  describe("numerals (row 10)", () => {
    it("day 0 reads 0 days past expiry, not 'to expiry'", () => {
      renderImm([live({ expiry_date: DAY })]);
      expect(screen.getByText("Days past expiry")).toBeInTheDocument();
      expect(screen.queryByText("Days to expiry")).not.toBeInTheDocument();
      expect(screen.getByText(/Expired today/)).toBeInTheDocument();
    });

    it("a past expiry reads days past, a future one days to expiry", () => {
      const past = renderImm([live({ expiry_date: plus(-5) })]);
      expect(screen.getByText("Days past expiry")).toBeInTheDocument();
      expect(screen.getByText("05")).toBeInTheDocument();
      past.unmount();
      renderImm([live({ expiry_date: plus(30) })]);
      expect(screen.getByText("Days to expiry")).toBeInTheDocument();
      expect(screen.getByText("30")).toBeInTheDocument();
    });

    it("GUILT: an unparseable expiry or issue date shows no numeral, never 0", () => {
      renderImm([
        live({ expiry_date: "nonsense", issue_date: "also nonsense" }),
      ]);
      expect(screen.queryByText("Days to expiry")).not.toBeInTheDocument();
      expect(screen.queryByText("Days past expiry")).not.toBeInTheDocument();
      expect(screen.queryByText("Days on this permit")).not.toBeInTheDocument();
      expect(screen.queryByRole("img")).not.toBeInTheDocument();
    });

    it("days on this permit counts from the issue date to the card's day", () => {
      renderImm([live({ issue_date: plus(-40), expiry_date: plus(300) })]);
      expect(screen.getByText("Days on this permit")).toBeInTheDocument();
      expect(screen.getByText("40")).toBeInTheDocument();
    });

    it("an issue date after the card's day reads 0 days on the permit", () => {
      renderImm([live({ issue_date: plus(5), expiry_date: plus(300) })]);
      const label = screen.getByText("Days on this permit");
      expect(label.previousElementSibling).toHaveTextContent("00");
    });
  });

  describe("urgency tone (row 12)", () => {
    const toneOf = () =>
      screen.getByText(/^Expires/).querySelector("b")?.className ?? "";

    it("an alert_color red at +200 days stays urgent", () => {
      renderImm([live({ expiry_date: plus(200), alert_color: "red" })]);
      expect(toneOf()).toContain("--state-warning");
    });

    it("a live expired permit with alert_color green is urgent", () => {
      renderImm([live({ expiry_date: plus(-4), alert_color: "green" })]);
      expect(toneOf()).toContain("--state-warning");
    });

    it("INNOCENCE: far away and green is quiet", () => {
      renderImm([live({ expiry_date: plus(200), alert_color: "green" })]);
      expect(toneOf()).not.toContain("--state-warning");
    });
  });

  describe("history text (row 16)", () => {
    const history = (older: Partial<ClientDocument>) =>
      renderImm([
        live({ expiry_date: plus(500), issue_date: plus(-100) }),
        doc({ id: 3, ...older }),
      ]);

    it("both dates: the period", () => {
      history({ issue_date: plus(-900), expiry_date: plus(-500) });
      expect(
        screen.getAllByText(`f:${plus(-900)} → f:${plus(-500)}`).length,
      ).toBeGreaterThan(0);
    });

    it("expiry only: 'Valid until' while valid or expiring, 'Ended' once expired (day 0 included)", () => {
      history({ expiry_date: plus(61) });
      expect(
        screen.getAllByText(`Valid until f:${plus(61)}`).length,
      ).toBeGreaterThan(0);
      cleanupAll();
      history({ expiry_date: plus(1) });
      expect(
        screen.getAllByText(`Valid until f:${plus(1)}`).length,
      ).toBeGreaterThan(0);
      cleanupAll();
      history({ expiry_date: DAY });
      expect(screen.getAllByText(`Ended f:${DAY}`).length).toBeGreaterThan(0);
    });

    it("undated: today's issue or upload text", () => {
      history({ issue_date: plus(-50), expiry_date: undefined });
      expect(
        screen.getAllByText(`Issued f:${plus(-50)}`).length,
      ).toBeGreaterThan(0);
    });
  });
});

// ---------------------------------------------------------------------------
// ValidityTrack (row 11)
// ---------------------------------------------------------------------------

describe("ValidityTrack contract", () => {
  const track = (issue: string | null, expiry: string | null, day = DAY) =>
    render(<ValidityTrack issueDate={issue} expiryDate={expiry} day={day} />);

  const label = () => screen.getByRole("img").getAttribute("aria-label");
  const fill = (container: HTMLElement) =>
    (container.querySelector("i") as HTMLElement).style.width;

  it("fills the elapsed share of the window", () => {
    const { container } = track(plus(-25), plus(75));
    expect(label()).toBe("25 of 100 days elapsed, 75 remaining");
    expect(fill(container)).toBe("25%");
  });

  it("clamps: a card day before the issue date is 0 elapsed", () => {
    const { container } = track(plus(10), plus(110));
    expect(label()).toBe("0 of 100 days elapsed, 100 remaining");
    expect(fill(container)).toBe("0%");
  });

  it("clamps: a card day after the expiry is fully elapsed", () => {
    const { container } = track(plus(-200), plus(-100));
    expect(label()).toBe("100 of 100 days elapsed, 0 remaining");
    expect(fill(container)).toBe("100%");
  });

  it("renders nothing when a date is null, unparseable, or the window is not positive", () => {
    expect(track(null, plus(10)).container).toBeEmptyDOMElement();
    expect(track(plus(-10), null).container).toBeEmptyDOMElement();
    expect(track("garbage", plus(10)).container).toBeEmptyDOMElement();
    expect(track(plus(10), plus(10)).container).toBeEmptyDOMElement();
    expect(track(plus(10), plus(-10)).container).toBeEmptyDOMElement();
  });
});

// ---------------------------------------------------------------------------
// Family tab and Overview family rows (rows 25-27)
// ---------------------------------------------------------------------------

const MEMBER = {
  id: 5,
  client_id: 1,
  full_name: "Family Member A",
  relationship: "spouse",
  current_visa_type: "D12",
} as FamilyMember;

const renderFamily = (
  members: FamilyMember[],
  documents: ClientDocument[] = [],
  onOpenRemoved?: () => void,
) =>
  render(
    <FamilyTab
      clientId={1}
      familyMembers={members}
      documents={documents}
      formatDate={fmt}
      onAddClick={vi.fn()}
      onEditClick={vi.fn()}
      onRefresh={vi.fn()}
      onOpenRemoved={onOpenRemoved}
      day={DAY}
    />,
  );

describe("Family tab (rows 25-26)", () => {
  it("GUILT: NULL visa_expiry, enum green, expired live fallback document — reads Expired", () => {
    renderFamily(
      [{ ...MEMBER, visa_expiry: undefined, visa_alert: "green" }],
      [doc({ id: 1, family_member_id: 5, expiry_date: plus(-10) })],
    );
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.queryByText("Valid")).not.toBeInTheDocument();
    expect(screen.getByText("Expired 10d ago")).toBeInTheDocument();
  });

  it("GUILT: enum expired with a model valid reads Expired", () => {
    renderFamily([
      { ...MEMBER, visa_expiry: plus(300), visa_alert: "expired" },
    ]);
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("GUILT: a rejected fallback document is never picked and never offered as a file", () => {
    const onOpen = vi.fn();
    renderFamily(
      [{ ...MEMBER, visa_expiry: undefined, visa_alert: undefined }],
      [
        doc({
          id: 1,
          family_member_id: 5,
          expiry_date: plus(300),
          status: "rejected",
          google_drive_file_url: "https://drive.google.com/file/d/x/view",
        }),
      ],
      onOpen,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText("Valid")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /View visa for/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: "1 removed or rejected file is in Documents",
      }),
    );
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("INNOCENCE: the same fallback document, live, is picked and its file is offered", () => {
    renderFamily(
      [{ ...MEMBER, visa_expiry: undefined, visa_alert: undefined }],
      [
        doc({
          id: 1,
          family_member_id: 5,
          expiry_date: plus(300),
          google_drive_file_url: "https://drive.google.com/file/d/x/view",
        }),
      ],
    );
    expect(screen.getByText("Valid")).toBeInTheDocument();
    expect(screen.getByText(`f:${plus(300)}`)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /View visa for/ }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });

  it("the passport window: urgent at 180 days with no enum, quiet at 181", () => {
    const first = renderFamily([
      { ...MEMBER, passport_expiry: plus(180), passport_alert: undefined },
    ]);
    expect(first.getByText(/Passport: /)).toHaveStyle({
      color: "var(--state-warning)",
    });
    first.unmount();
    const second = renderFamily([
      { ...MEMBER, passport_expiry: plus(181), passport_alert: undefined },
    ]);
    expect(second.getByText(/Passport: /)).toHaveStyle({
      color: "var(--tx-secondary)",
    });
  });
});

describe("Overview family rows (row 27) render the same result", () => {
  const baseProps = {
    client: { id: 1, full_name: "Sample Client" } as ClientProfile["client"],
    stats: {
      family_count: 1,
      documents_count: 0,
      practices_count: 0,
      expired_count: 0,
      red_alerts: 0,
      yellow_alerts: 0,
    },
    activePractices: [],
    completedPractices: [],
    needsViewerAction: false,
    formatDate: fmt,
    formatCurrency: String,
    onEditClick: vi.fn(),
    onDeleteClick: vi.fn(),
    canDeleteClient: false,
    isDeletingClient: false,
    onRefresh: vi.fn().mockResolvedValue(undefined),
    clientId: 1,
    day: DAY,
  };

  const familyRow = (_container: HTMLElement) =>
    screen
      .getByText("Family Member A")
      .closest("button")!
      .querySelector("span.shrink-0")!;

  const family = (member: FamilyMember, documents: ClientDocument[]) =>
    render(
      <OverviewTab
        {...baseProps}
        documents={documents}
        familyMembers={[member]}
      />,
    );

  it("GUILT: NULL visa_expiry, enum green and an expired live fallback — urgent on the Overview too", () => {
    const { container } = family(
      { ...MEMBER, visa_expiry: undefined, visa_alert: "green" },
      [doc({ id: 1, family_member_id: 5, expiry_date: plus(-10) })],
    );
    const row = familyRow(container);
    expect(row.className).toContain("--state-warning");
    expect(row.textContent).toMatch(/D12 · /);
  });

  it("GUILT: a rejected fallback document gives the Overview row no date at all", () => {
    const { container } = family(
      { ...MEMBER, visa_expiry: undefined, visa_alert: "red" },
      [
        doc({
          id: 1,
          family_member_id: 5,
          expiry_date: plus(-10),
          status: "rejected",
        }),
      ],
    );
    const row = familyRow(container);
    expect(row.className).not.toContain("--state-warning");
    expect(row.textContent).toBe("D12");
  });

  it("INNOCENCE: a live far-away permit is quiet", () => {
    const { container } = family(
      { ...MEMBER, visa_expiry: plus(400), visa_alert: "green" },
      [],
    );
    const row = familyRow(container);
    expect(row.className).not.toContain("--state-warning");
  });
});

// ---------------------------------------------------------------------------
// Visa and Passport cards (rows 3-7, 9)
// ---------------------------------------------------------------------------

const CLIENT = {
  id: 42,
  full_name: "Sample Client",
} as ClientProfile["client"];

const renderVisa = (
  documents: ClientDocument[],
  extra: { onOpenRemoved?: () => void; day?: string } = {},
) =>
  render(
    <VisaCard
      client={CLIENT}
      clientId={42}
      documents={documents}
      activePractices={[]}
      formatDate={fmt}
      formatCurrency={String}
      onRefresh={vi.fn()}
      onOpenRemoved={extra.onOpenRemoved}
      day={extra.day ?? DAY}
    />,
  );

describe("Visa card (rows 3, 5, 9)", () => {
  it("day 0 reads Expired today, in the warning tone", () => {
    renderVisa([doc({ id: 1, expiry_date: DAY, issue_date: plus(-100) })]);
    const text = screen.getByText("Expired today");
    expect(text.className).toContain("--state-warning");
  });

  it("day 1 is 1 day left, in the soon tone", () => {
    renderVisa([doc({ id: 1, expiry_date: plus(1) })]);
    expect(screen.getByText("1 day left")).toBeInTheDocument();
  });

  it("GUILT: a rejected permit is never current — no countdown, no runway; the text and signpost say why", () => {
    const onOpen = vi.fn();
    renderVisa(
      [
        doc({
          id: 1,
          status: "rejected",
          expiry_date: plus(300),
          issue_date: plus(-100),
        }),
      ],
      { onOpenRemoved: onOpen },
    );
    expect(screen.queryByText(/days left/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("permit-runway")).not.toBeInTheDocument();
    expect(screen.getByText(/No valid visa or KITAS on file/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", {
        name: "1 removed or rejected file is in Documents",
      }),
    );
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("INNOCENCE: with no void permit the text and the absence of a signpost are today's", () => {
    renderVisa([]);
    expect(screen.getByText(/No visa or KITAS on file\./)).toBeInTheDocument();
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });

  it("INNOCENCE: the same permit, live, is current with its countdown and runway", () => {
    renderVisa([
      doc({ id: 1, expiry_date: plus(300), issue_date: plus(-100) }),
    ]);
    expect(screen.getByText("300 days left")).toBeInTheDocument();
    expect(screen.getByTestId("permit-runway")).toBeInTheDocument();
  });

  it("with a live permit and a rejected one, the live one is current and the signpost still shows", () => {
    renderVisa([
      doc({ id: 1, status: "rejected", expiry_date: plus(900) }),
      doc({ id: 2, expiry_date: plus(300) }),
    ]);
    expect(screen.getByText("300 days left")).toBeInTheDocument();
    expect(
      screen.getByText("1 removed or rejected file is in Documents"),
    ).toBeInTheDocument();
  });
});

describe("Passport card (rows 4, 5)", () => {
  const passportDoc = (over: Partial<ClientDocument> & { id: number }) =>
    doc({
      document_type: "passport",
      document_category: "personal",
      file_name: `passport-${over.id}.jpg`,
      google_drive_file_url: "https://drive.google.com/file/d/f/view",
      ...over,
    });

  const renderPassport = (
    expiry: string | null,
    documents: ClientDocument[] = [],
    onOpenRemoved?: () => void,
  ) =>
    render(
      <PassportCard
        client={
          {
            ...CLIENT,
            passport_number: "X0000001",
            passport_expiry: expiry,
          } as unknown as ClientProfile["client"]
        }
        documents={documents}
        formatDate={fmt}
        onRefresh={vi.fn()}
        clientId={42}
        onOpenRemoved={onOpenRemoved}
        day={DAY}
      />,
    );

  it.each([
    [-1, "expired"],
    [0, "expired"],
    [1, "critical"],
    [269, "critical"],
    [270, "warning"],
    [419, "warning"],
    [420, "ok"],
  ])("daysLeft %i -> alert %s", (n, level) => {
    renderPassport(plus(n));
    const alert = screen.queryByTestId("passport-alert");
    if (level === "ok") expect(alert).toBeNull();
    else if (level === "expired")
      expect(alert).toHaveTextContent("This passport has expired");
    else if (level === "critical")
      expect(alert).toHaveTextContent("Expires very soon");
    else expect(alert).toHaveTextContent("Inside the 13-month window");
  });

  it("day 0 reads Expired today", () => {
    renderPassport(DAY);
    expect(screen.getByText("Expired today")).toBeInTheDocument();
  });

  it("GUILT: a rejected passport scan is not the current one — no 'Scan on file', the signpost shows", () => {
    const onOpen = vi.fn();
    renderPassport(
      plus(500),
      [passportDoc({ id: 1, status: "rejected" })],
      onOpen,
    );
    expect(screen.queryByTestId("passport-file")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: "1 removed or rejected file is in Documents",
      }),
    );
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("INNOCENCE: the same scan, live, is the current passport", () => {
    renderPassport(plus(500), [passportDoc({ id: 1 })]);
    expect(screen.getByTestId("passport-file")).toHaveTextContent("passport-1");
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Local ledger Visa row (row 7)
// ---------------------------------------------------------------------------

describe("Local ledger Visa row (row 7)", () => {
  const ledger = (documents: ClientDocument[]) =>
    buildLedger(undefined, documents, dayToInstant(DAY));

  it("day 0 is Expired '0 days ago', day 1 is Expiring, 61 is Valid", () => {
    expect(ledger([doc({ id: 1, expiry_date: DAY })]).rows[0]).toMatchObject({
      word: "Expired",
      tone: "strong",
    });
    expect(ledger([doc({ id: 1, expiry_date: DAY })]).rows[0].title).toContain(
      "expired 0 days ago",
    );
    expect(ledger([doc({ id: 1, expiry_date: plus(1) })]).rows[0].word).toBe(
      "Expiring",
    );
    expect(ledger([doc({ id: 1, expiry_date: plus(60) })]).rows[0].word).toBe(
      "Expiring",
    );
    expect(ledger([doc({ id: 1, expiry_date: plus(61) })]).rows[0].word).toBe(
      "Valid",
    );
  });

  it("GUILT: a client with only a rejected permit has no Visa row", () => {
    expect(
      ledger([doc({ id: 1, status: "rejected", expiry_date: plus(30) })]).rows,
    ).toHaveLength(0);
  });

  it("INNOCENCE: the same permit, live, has one", () => {
    expect(ledger([doc({ id: 1, expiry_date: plus(30) })]).rows).toHaveLength(
      1,
    );
  });
});

// ---------------------------------------------------------------------------
// ProcessTab chip (row 28)
// ---------------------------------------------------------------------------

describe("ProcessTab expiry chip (row 28)", () => {
  const practice = (over: Record<string, unknown>) =>
    ({
      id: 1,
      status: "on_process",
      practice_type_code: "KITAS",
      practice_type_name: "KITAS",
      ...over,
    }) as ClientProfile["practices"][number];

  const chip = (p: ClientProfile["practices"][number]) => {
    const view = render(
      <ProcessTab
        clientId={1}
        practices={[p]}
        formatDate={fmt}
        onRefresh={vi.fn()}
        day={DAY}
      />,
    );
    return view;
  };

  it.each([
    [-1, /exp 1d ago/],
    [0, /expires today/],
    [1, /⏰ 1d left/],
    [14, /⏰ 14d left/],
    [15, /⏰ 15d left/],
    [30, /⏰ 30d left/],
    [31, /⏰ 31d left/],
  ])("daysLeft %i reads %s", (n, text) => {
    chip(practice({ expiry_date: plus(n) }));
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each([
    [-1, true],
    [0, true],
    [14, true],
    [15, true],
    [30, true],
    [31, false],
  ])("daysLeft %i is in the warning tone: %s", (n, warn) => {
    chip(practice({ expiry_date: plus(n), alert_color: "green" }));
    const el = screen.getByText(/exp|expires|left/).closest("div");
    expect(el?.className.includes("--state-warning") ?? false).toBe(warn);
  });

  it("alert_color keeps its fallback beyond 30 days", () => {
    chip(practice({ expiry_date: plus(60), alert_color: "red" }));
    const el = screen.getByText(/⏰ 60d left/).closest("div");
    expect(el?.className ?? "").not.toContain("--state-warning/10 text");
  });

  it("an unparseable date shows no chip", () => {
    chip(practice({ expiry_date: "someday" }));
    expect(screen.queryByText(/left|exp /)).not.toBeInTheDocument();
  });

  it("a date followed by a time is read as written, no timezone conversion", () => {
    chip(practice({ expiry_date: `${plus(10)}T23:30:00-11:00` }));
    expect(screen.getByText(/⏰ 10d left/)).toBeInTheDocument();
  });
});

beforeEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Obligation 5: a date-only expiry reads the same calendar day on every
// surface, whatever the browser's timezone.
// ---------------------------------------------------------------------------

describe("the same calendar day on every surface, in every browser timezone", () => {
  const EXPIRY = "2027-03-31";
  const real = (d: string) => formatCardDate(d, true);

  it.each(BROWSER_ZONES)("browser in %s", (tz) => {
    asBrowserIn(tz, () => {
      // Documents cell
      const docs = render(
        <DocumentsTab
          clientId={1}
          documents={[
            doc({
              id: 1,
              document_type: "bank_statement",
              document_category: "other",
              expiry_date: EXPIRY,
            }),
          ]}
          documentsByCategory={{ other: [] }}
          formatDate={real}
          onAddClick={vi.fn()}
          onEditClick={vi.fn()}
          onRefresh={vi.fn()}
          day={DAY}
        />,
      );
      expect(docs.container.textContent).toContain("31 Mar 2027");
      docs.unmount();

      // Immigration history text and the permit panel
      const imm = render(
        <ImmigrationTab
          clientId={1}
          documents={[
            doc({ id: 1, expiry_date: "2028-03-31" }),
            doc({ id: 2, expiry_date: EXPIRY }),
          ]}
          formatDate={real}
          onAddClick={vi.fn()}
          onEditClick={vi.fn()}
          onRefresh={vi.fn()}
          day={DAY}
        />,
      );
      expect(imm.container.textContent).toContain("Valid until 31 Mar 2027");
      expect(imm.container.textContent).toContain("31 Mar 2028");
      imm.unmount();

      // Family row
      const fam = render(
        <FamilyTab
          clientId={1}
          familyMembers={[{ ...MEMBER, visa_expiry: EXPIRY }]}
          documents={[]}
          formatDate={real}
          onAddClick={vi.fn()}
          onEditClick={vi.fn()}
          onRefresh={vi.fn()}
          day={DAY}
        />,
      );
      expect(fam.container.textContent).toContain("31 Mar 2027");
      fam.unmount();

      // Visa card
      const visa = render(
        <VisaCard
          client={CLIENT}
          clientId={42}
          documents={[doc({ id: 1, expiry_date: EXPIRY })]}
          activePractices={[]}
          formatDate={real}
          formatCurrency={String}
          onRefresh={vi.fn()}
          day={DAY}
        />,
      );
      expect(visa.container.textContent).toContain("31 Mar 2027");
      visa.unmount();

      // Process chip: the day count does not move with the zone
      const proc = render(
        <ProcessTab
          clientId={1}
          practices={[
            {
              id: 1,
              status: "on_process",
              practice_type_code: "KITAS",
              practice_type_name: "KITAS",
              expiry_date: "2026-11-27",
            } as ClientProfile["practices"][number],
          ]}
          formatDate={real}
          onRefresh={vi.fn()}
          day={DAY}
        />,
      );
      expect(proc.container.textContent).toContain("⏰ 20d left");
      proc.unmount();
    });
  });
});
