import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { ClientRequiredDocument } from "@/lib/types/required-documents";
import { dayMonth } from "@/lib/document-request-format";

const { mockGetProfile, mockListMatters, mockGetMyRequiredDocuments } =
  vi.hoisted(() => ({
    mockGetProfile: vi.fn(),
    mockListMatters: vi.fn(),
    mockGetMyRequiredDocuments: vi.fn(),
  }));

vi.mock("next/dynamic", () => ({
  default: () => () => null,
}));

vi.mock("@/components/portal", () => ({
  ProcessStepper: () => <div>Timeline</div>,
}));

// The real statusToBaton stays; only the component is replaced, by a stub that shows the
// label of the button, so "the button names the first waiting row" can be asserted.
vi.mock("@/components/portal/PracticeBaton", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/portal/PracticeBaton")
  >("@/components/portal/PracticeBaton");
  return {
    ...actual,
    PracticeBaton: ({ nextActionLabel }: { nextActionLabel?: string }) => (
      <div data-testid="baton">{nextActionLabel ?? ""}</div>
    ),
  };
});

vi.mock("@/hooks/usePortalProcessTimeline", () => ({
  usePortalProcessTimeline: () => ({ data: undefined, isLoading: false }),
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: { open: boolean; children: React.ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => (
    <div role="dialog">{children}</div>
  ),
  DialogHeader: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  DialogTitle: ({ children }: { children: React.ReactNode }) => (
    <h2>{children}</h2>
  ),
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));

vi.mock("@/lib/api", () => ({
  api: {
    portal: {
      getProfile: mockGetProfile,
      listMatters: mockListMatters,
      getMyRequiredDocuments: mockGetMyRequiredDocuments,
    },
    crm: { uploadClientDocument: vi.fn() },
  },
}));

// Midday UTC, so the day does not depend on the zone of the machine.
const ADDED_AT = "2026-11-28T12:00:00+00:00";

const seeded: ClientRequiredDocument = {
  id: 10,
  practice_id: 603,
  process_name: "Zz Test Visa",
  process_status: "waiting_documents",
  document_type: "zz_seeded",
  document_label: "Zz Seeded Document",
  description: "Seeded description",
  is_required: true,
  uploaded_by_client: false,
  status: "pending",
  client_notes: null,
};

const asked: ClientRequiredDocument = {
  ...seeded,
  id: 11,
  document_type: "zz_asked",
  document_label: "Zz Asked Document",
  description: "Asked description",
  case_request: { added_by: "Zz Maya", added_at: ADDED_AT },
};

async function renderPage(documents: ClientRequiredDocument[]) {
  mockGetMyRequiredDocuments.mockResolvedValue(documents);
  const { default: PortalProcessPage } = await import("./page");
  render(<PortalProcessPage />);
  await screen.findByText(documents[0].document_label);
}

function rowOf(label: string): HTMLElement {
  const row = screen.getByText(label).closest("[id^='doc-']");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

describe("PortalProcessPage: who added this document for your case", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetProfile.mockResolvedValue({ id: 11898, fullName: "Zz Client" });
    mockListMatters.mockResolvedValue({
      matters: [
        {
          id: 603,
          title: "Zz Test Visa",
          status: "waiting_documents",
          type: "visa",
          progress: 10,
          pending_docs: [],
          next_deadline: null,
          next_step: "waiting_documents",
        },
      ],
    });
  });

  it("shows the name and the day on the row of its document and on no other row", async () => {
    await renderPage([asked, seeded]);
    expect(
      within(rowOf("Zz Asked Document")).getByText(
        "Zz Maya added this for your case · 28 Nov",
      ),
    ).toBeInTheDocument();
    expect(
      within(rowOf("Zz Seeded Document")).queryByText(/for your case/),
    ).toBeNull();
    expect(screen.getAllByText(/for your case/)).toHaveLength(1);
  });

  it("reads 'Added for your case' when added_by is null", async () => {
    await renderPage([
      { ...asked, case_request: { added_by: null, added_at: ADDED_AT } },
    ]);
    expect(
      screen.getByText("Added for your case · 28 Nov"),
    ).toBeInTheDocument();
  });

  it("reads 'Added for your case' when added_by is blank", async () => {
    await renderPage([
      { ...asked, case_request: { added_by: "   ", added_at: ADDED_AT } },
    ]);
    expect(
      screen.getByText("Added for your case · 28 Nov"),
    ).toBeInTheDocument();
  });

  it("leaves out the separator and the date when the date is not one", async () => {
    await renderPage([
      { ...asked, case_request: { added_by: "Zz Maya", added_at: "" } },
    ]);
    expect(
      screen.getByText("Zz Maya added this for your case"),
    ).toBeInTheDocument();
  });

  it("shows no line when the key is absent", async () => {
    await renderPage([seeded]);
    expect(screen.queryByText(/for your case/)).toBeNull();
  });

  it("shows no line when case_request is null", async () => {
    await renderPage([{ ...asked, case_request: null }]);
    expect(screen.queryByText(/for your case/)).toBeNull();
  });

  it.each(["pending", "uploaded", "rejected"] as const)(
    "shows the line for a %s slot",
    async (status) => {
      await renderPage([{ ...asked, status }]);
      expect(
        screen.getByText(/Zz Maya added this for your case/),
      ).toBeInTheDocument();
    },
  );

  it("shows the line for a document with no description", async () => {
    await renderPage([{ ...asked, description: null }]);
    expect(
      screen.getByText("Zz Maya added this for your case · 28 Nov"),
    ).toBeInTheDocument();
  });

  it("names the first waiting row the server sent on the button of a practice in waiting_documents", async () => {
    await renderPage([asked, seeded]);
    const labels = screen
      .getAllByTestId("baton")
      .map((baton) => baton.textContent);
    expect(labels).toContain("Upload: Zz Asked Document");
    expect(labels).not.toContain("Upload: Zz Seeded Document");
  });

  it("keeps the line small and muted, under the description", async () => {
    await renderPage([asked]);
    const line = screen.getByText(/Zz Maya added this for your case/);
    expect(line.tagName).toBe("P");
    expect(line.className).toContain("text-xs");
    expect(line.previousElementSibling?.textContent).toBe("Asked description");
  });
});

describe("dayMonth", () => {
  it("formats the day in the zone given", () => {
    expect(dayMonth("2026-11-28T23:00:00+00:00", "UTC")).toBe("28 Nov");
    expect(dayMonth("2026-11-28T23:00:00+00:00", "Asia/Makassar")).toBe(
      "29 Nov",
    );
  });

  it("writes every month with three letters, September included", () => {
    expect(dayMonth("2026-09-05T12:00:00+00:00", "UTC")).toBe("5 Sep");
    expect(dayMonth("2026-06-05T12:00:00+00:00", "UTC")).toBe("5 Jun");
  });

  it("gives an empty string for a string that is not a date and for null", () => {
    expect(dayMonth("not a date")).toBe("");
    expect(dayMonth(null)).toBe("");
  });
});
