import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewTab } from "./OverviewTab";
import { api } from "@/lib/api";
import type { ClientProfile } from "@/lib/api/crm/crm.types";

// OverviewTab also mounts AiSummaryCard / WaCaseIntelligencePanel, both of
// which fetch on mount — stub them to their real "not generated" shape so
// they render their quiet empty state instead of throwing.
vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getClientAiSummary: vi.fn(),
      getClientWaCaseIntelligence: vi.fn(),
      // CRM-18: present so a test can assert this component never reaches it.
      deleteClient: vi.fn(),
      extractPassportForClient: vi.fn(),
    },
    post: vi.fn(),
    request: vi.fn(),
  },
}));

// The vault mounts the portal panels, which poll their own endpoints; they
// have their own suites (PortalAccess / PortalMessages / VaultCard).
vi.mock("./VaultCard", () => ({
  VaultCard: () => <div data-testid="vault-card" />,
}));

const client = {
  id: 42,
  uuid: "test-uuid",
  full_name: "Test Client",
  status: "active",
  client_type: "individual",
  date_of_birth: undefined,
} as unknown as ClientProfile["client"];

const stats = {
  family_count: 0,
  documents_count: 0,
  practices_count: 0,
  expired_count: 0,
  red_alerts: 0,
  yellow_alerts: 0,
} as ClientProfile["stats"];

const baseProps = {
  client,
  stats,
  documents: [],
  activePractices: [],
  completedPractices: [],
  formatDate: (d: string) => d,
  formatCurrency: (n: number) => String(n),
  onEditClick: vi.fn(),
  // CRM-18: the delete affordance is OFF by default here, so every pre-existing
  // case in this file keeps asserting the desk it asserted before. The two
  // cases that care turn it on explicitly.
  onDeleteClick: vi.fn(),
  canDeleteClient: false,
  isDeletingClient: false,
  onRefresh: vi.fn().mockResolvedValue(undefined),
  clientId: 42,
};

/**
 * CRM-18 — the delete affordance is rendered only when the parent says the
 * viewer may have it. `viewerCanDeleteClient` decides that (pinned in
 * ../__tests__/delete-client-permission.guard.test.ts); these two cases pin
 * that OverviewTab actually obeys the answer instead of always painting the
 * button and leaning on the backend's 403 to hide the mistake.
 */
describe("OverviewTab — delete client affordance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
      status: "not_generated",
    } as any);
    vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
      status: "not_generated",
      cases: [],
    } as any);
  });

  it("GUILT: renders no delete button when the viewer may not delete", () => {
    render(
      <OverviewTab
        {...baseProps}
        needsViewerAction={false}
        canDeleteClient={false}
      />,
    );
    expect(screen.queryByLabelText("Delete client")).toBeNull();
    // The edit affordance is untouched — this gate is about one button.
    expect(screen.getByLabelText("Edit client info")).toBeTruthy();
  });

  it("INNOCENCE: renders the delete button, and it asks the parent to confirm", () => {
    const onDeleteClick = vi.fn();
    render(
      <OverviewTab
        {...baseProps}
        needsViewerAction={false}
        canDeleteClient
        onDeleteClick={onDeleteClick}
      />,
    );
    const button = screen.getByLabelText("Delete client");
    fireEvent.click(button);
    // It never calls the API itself: the confirmation and the request both
    // belong to ClientDetailClient.
    expect(onDeleteClick).toHaveBeenCalledTimes(1);
    expect(api.crm.deleteClient).not.toHaveBeenCalled();
  });

  it("GUILT: the button is disabled while a delete is already in flight", () => {
    render(
      <OverviewTab
        {...baseProps}
        needsViewerAction={false}
        canDeleteClient
        isDeletingClient
      />,
    );
    expect(
      screen.getByLabelText("Delete client").hasAttribute("disabled"),
    ).toBe(true);
  });
});

describe("OverviewTab — company document count (K22)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
      status: "not_generated",
    } as any);
    vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
      status: "not_generated",
      cases: [],
    } as any);
  });

  const link = {
    link_id: 1,
    company_id: 9,
    company_name: "PT Sample",
    company_type: "PT PMA",
    role: "director",
    is_primary: true,
    status: "active",
  } as any;

  it("GUILT: a client with linked companies sees the company document count", () => {
    render(
      <OverviewTab
        {...baseProps}
        companyLinks={[link]}
        stats={{
          ...stats,
          documents_count: 3,
          company_count: 2,
          company_documents_count: 5,
        }}
        needsViewerAction={false}
      />,
    );
    expect(screen.getByTestId("company-docs-count").textContent).toBe(
      "5 company documents on file",
    );
    expect(screen.getByText("PT Sample")).toBeTruthy();
  });

  it("INNOCENCE: no linked company renders no company document count", () => {
    render(
      <OverviewTab
        {...baseProps}
        stats={{ ...stats, company_count: 0, company_documents_count: 0 }}
        needsViewerAction={false}
      />,
    );
    expect(screen.queryByTestId("company-docs-count")).toBeNull();
  });

  it("INNOCENCE: a response without the field renders no count instead of 0", () => {
    render(
      <OverviewTab
        {...baseProps}
        stats={{ ...stats, company_count: 1 }}
        needsViewerAction={false}
      />,
    );
    expect(screen.queryByTestId("company-docs-count")).toBeNull();
  });
});

describe("OverviewTab — open processes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
      status: "not_generated",
    } as any);
    vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
      status: "not_generated",
      cases: [],
    } as any);
  });

  const practice = (over: Record<string, unknown>) =>
    ({
      id: 1,
      status: "waiting_documents",
      practice_type_code: "KITAS_RENEWAL",
      practice_type_name: "KITAS Renewal",
      ...over,
    }) as ClientProfile["practices"][number];

  const processesSection = () =>
    screen.getByRole("heading", { name: "Open processes" }).closest("section")!;

  it("GUILT: a partially paid practice shows what is still owed, not its full price", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[
          practice({
            actual_price: 14500000,
            paid_amount: 7000000,
            payment_status: "partial",
          }),
        ]}
        needsViewerAction={false}
      />,
    );
    expect(
      within(processesSection()).getByText("7500000 outstanding"),
    ).toBeTruthy();
    expect(
      within(processesSection()).queryByText("14500000 outstanding"),
    ).toBeNull();
  });

  it("GUILT: the viewer's own practice reads You; someone else's names that person", () => {
    render(
      <OverviewTab
        {...baseProps}
        viewerEmail="me@balizero.com"
        activePractices={[
          practice({ id: 1, assigned_to: "me@balizero.com" }),
          practice({
            id: 2,
            practice_type_name: "Annual SPT",
            assigned_to: "angel@balizero.com",
          }),
        ]}
        needsViewerAction={false}
      />,
    );
    expect(within(processesSection()).getByText("You")).toBeTruthy();
    expect(within(processesSection()).getByText("angel")).toBeTruthy();
  });

  it("INNOCENCE: an unknown viewer never claims You", () => {
    render(
      <OverviewTab
        {...baseProps}
        viewerEmail={null}
        activePractices={[practice({ assigned_to: "me@balizero.com" })]}
        needsViewerAction={false}
      />,
    );
    expect(screen.queryByText("You")).toBeNull();
  });

  it("INNOCENCE: no active practice renders no Open processes ledger", () => {
    render(<OverviewTab {...baseProps} needsViewerAction={false} />);
    expect(
      screen.queryByRole("heading", { name: "Open processes" }),
    ).toBeNull();
  });

  it("the Client info card says so when no contact was ever logged", () => {
    render(<OverviewTab {...baseProps} needsViewerAction={false} />);
    expect(screen.getByTestId("client-last-contact").textContent).toContain(
      "No interaction logged yet",
    );
  });

  it("INNOCENCE: no permit and an open KITAS practice says Visa in process, never Renewal", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[practice({})]}
        needsViewerAction={false}
      />,
    );
    const visaCard = screen.getByTestId("visa-card").textContent!;
    expect(visaCard).toContain("Visa in process");
    expect(visaCard).not.toContain("Renewal");
    expect(visaCard).not.toContain("No visa or KITAS on file");
  });

  it("GUILT: fully paid open practices still show the pipeline, with no outstanding text", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[
          practice({ actual_price: "14500000.00", payment_status: "paid" }),
        ]}
        needsViewerAction={false}
      />,
    );
    expect(
      within(processesSection()).getByText("14500000 pipeline"),
    ).toBeTruthy();
    expect(within(processesSection()).queryByText(/outstanding/)).toBeNull();
  });

  it("INNOCENCE: zero-priced open practices show no pipeline", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[practice({})]}
        needsViewerAction={false}
      />,
    );
    expect(within(processesSection()).queryByText(/pipeline/)).toBeNull();
  });
});

describe("OverviewTab — money line sits in the body, not the one-row head (gate round 2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
      status: "not_generated",
    } as any);
    vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
      status: "not_generated",
      cases: [],
    } as any);
  });

  it("GUILT: pipeline and outstanding render in the wrapping body line, never beside the heading", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[
          {
            id: 1,
            status: "in_progress",
            practice_type_code: "KITAS_RENEWAL",
            practice_type_name: "KITAS Renewal",
            actual_price: 14500000,
            paid_amount: 7000000,
            payment_status: "partial",
          } as ClientProfile["practices"][number],
        ]}
        needsViewerAction={false}
      />,
    );
    const heading = screen.getByRole("heading", { name: "Open processes" });
    const headRow = heading.parentElement!;
    expect(headRow.textContent).not.toMatch(/pipeline|outstanding/);
    const money = screen.getByTestId("processes-money");
    expect(money.textContent).toContain("14500000 pipeline");
    expect(money.textContent).toContain("7500000 outstanding");
    expect(money.className).toContain("flex-wrap");
  });

  it("GUILT: a partial practice whose record has no paid_amount reads 'balance not recorded', never the full price as outstanding", () => {
    render(
      <OverviewTab
        {...baseProps}
        activePractices={[
          {
            id: 1,
            status: "in_progress",
            practice_type_code: "KITAS_RENEWAL",
            practice_type_name: "KITAS Renewal",
            actual_price: 14500000,
            payment_status: "partial",
          } as ClientProfile["practices"][number],
        ]}
        needsViewerAction={false}
      />,
    );
    const money = screen.getByTestId("processes-money");
    expect(money.textContent).toContain("balance not recorded");
    expect(money.textContent).not.toContain("outstanding");
    expect(screen.getByText(/Balance not recorded/)).toBeInTheDocument();
    expect(screen.queryByText(/14500000 due/)).not.toBeInTheDocument();
  });
});

describe("OverviewTab — gate round 3", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
      status: "not_generated",
    } as any);
    vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
      status: "not_generated",
      cases: [],
    } as any);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const kitas = {
    id: 7,
    client_id: 42,
    document_type: "kitas",
    document_category: "immigration",
    expiry_date: "2026-11-03",
  } as any;

  it("GUILT: an open tab crosses WITA midnight — expiring today becomes expired without a remount", () => {
    vi.useFakeTimers();
    // 23:59:30 WITA on 3 Nov 2026 == 15:59:30Z
    vi.setSystemTime(new Date("2026-11-03T15:59:30Z"));
    render(
      <OverviewTab
        {...baseProps}
        documents={[kitas]}
        needsViewerAction={false}
      />,
    );
    const card = () => screen.getByTestId("visa-card").textContent;
    expect(card()).toContain("Expires today");
    act(() => {
      vi.advanceTimersByTime(61_000);
    });
    expect(card()).toContain("Expired 1 day ago");
  });

  it("GUILT: the strategic recap names its source when the record carries one", () => {
    render(
      <OverviewTab
        {...baseProps}
        client={
          {
            ...client,
            strategic_recap: "Sample recap",
            strategic_recap_source: "auto_summary",
          } as any
        }
        needsViewerAction={false}
      />,
    );
    expect(screen.getByTestId("recap-source").textContent).toBe("auto summary");
  });

  it("INNOCENCE: no source on record renders no source label", () => {
    render(
      <OverviewTab
        {...baseProps}
        client={{ ...client, strategic_recap: "Sample recap" } as any}
        needsViewerAction={false}
      />,
    );
    expect(screen.getByText("Sample recap")).toBeTruthy();
    expect(screen.queryByTestId("recap-source")).toBeNull();
  });
});
