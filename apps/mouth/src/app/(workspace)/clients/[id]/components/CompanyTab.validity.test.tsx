/**
 * Company vault (rows 32, 33): a void PMA document leaves its slot empty and
 * is not counted as "on file"; its live twin fills the slot and is counted.
 * Synthetic fixtures only.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompanyTab } from "./CompanyTab";
import { clearCompanyDetailCache } from "./company/companyDetailCache";
import type {
  ClientCompanyLink,
  ClientDocument,
  ClientProfile,
} from "@/lib/api/crm/crm.types";

const { mockGetClientCompanies, mockGetCompany } = vi.hoisted(() => ({
  mockGetClientCompanies: vi.fn(),
  mockGetCompany: vi.fn().mockResolvedValue({ associates: [], documents: [] }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getClientCompanies: mockGetClientCompanies,
      getCompany: mockGetCompany,
      searchCompanyByName: vi.fn().mockResolvedValue(null),
      getCompanyDocuments: vi.fn().mockResolvedValue([]),
      getClientRecap: vi.fn().mockRejectedValue(new Error("no recap")),
    },
    post: vi.fn(),
  },
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));
vi.mock("./AiSummaryCard", () => ({ AiSummaryCard: () => null }));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

const CLIENT = {
  id: 412,
  full_name: "Client A",
} as unknown as ClientProfile["client"];

const LINK = {
  link_id: 1,
  company_id: 90,
  company_name: "PT Synthetic Studio",
  company_type: "PT",
  role: "director",
  is_primary: true,
  status: "active",
  company_status: "active",
} as ClientCompanyLink;

const pma = (
  over: Partial<ClientDocument> & { id: number },
): ClientDocument => ({
  client_id: 412,
  document_type: "nib",
  document_category: "pma",
  file_name: `nib-${over.id}.pdf`,
  file_id: `drive-${over.id}`,
  google_drive_file_url: `https://drive.google.com/file/d/drive-${over.id}/view`,
  ...over,
});

const mount = (documents: ClientDocument[], onOpenRemoved = vi.fn()) =>
  render(
    <CompanyTab
      clientId={412}
      client={CLIENT}
      documents={documents}
      formatDate={(d) => d}
      onRefresh={() => Promise.resolve()}
      onOpenRemoved={onOpenRemoved}
    />,
  );

beforeEach(() => {
  clearCompanyDetailCache();
  mockGetClientCompanies.mockResolvedValue([LINK]);
  mockGetCompany.mockResolvedValue({ associates: [], documents: [] });
});
afterEach(() => vi.clearAllMocks());

describe("Company vault — slots (row 32)", () => {
  it.each([
    ["rejected", { status: "rejected" }],
    ["removed", { deleted_at: "2026-10-01T10:00:00Z" }],
    ["archived", { is_archived: true }],
  ] as const)(
    "GUILT: a %s PMA document leaves its slot empty and shows the signpost",
    async (_n, fields) => {
      const onOpen = vi.fn();
      mount([pma({ id: 1, ...fields } as never)], onOpen);
      await screen.findByText("Akta Pendirian");
      expect(screen.queryByText("nib-1.pdf")).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", {
          name: "1 removed or rejected file is in Documents",
        }),
      );
      expect(onOpen).toHaveBeenCalledOnce();
    },
  );

  it("INNOCENCE: the same document, live, fills its slot and there is no signpost", async () => {
    mount([pma({ id: 1 })]);
    expect(await screen.findByText("nib-1.pdf")).toBeInTheDocument();
    expect(screen.queryByTestId("removed-signpost")).not.toBeInTheDocument();
  });

  it("with a void and a live NIB document, the live one fills the slot", async () => {
    mount([
      pma({ id: 1, status: "rejected" }),
      pma({ id: 2, file_name: "nib-live.pdf" }),
    ]);
    expect(await screen.findByText("nib-live.pdf")).toBeInTheDocument();
    expect(screen.queryByText("nib-1.pdf")).not.toBeInTheDocument();
  });
});

describe("Company — 'N documents on file' and the vault count (row 33)", () => {
  it("GUILT: a rejected or removed PMA document is not on file", async () => {
    mount([
      pma({ id: 1, status: "rejected" }),
      pma({ id: 2, deleted_at: "2026-10-01T10:00:00Z" }),
    ]);
    await screen.findByText("Akta Pendirian");
    expect(screen.queryByText(/documents? on file/)).not.toBeInTheDocument();
    const vault = screen.getByText(/Document vault/).closest("div")!;
    expect(within(vault).getByText("00")).toBeInTheDocument();
  });

  it("INNOCENCE: the live twin is counted, in the line and in the vault heading", async () => {
    mount([pma({ id: 1, status: "rejected" }), pma({ id: 2 })]);
    expect(await screen.findByText(/1 document on file/)).toBeInTheDocument();
    const vault = screen.getByText(/Document vault/).closest("div")!;
    expect(within(vault).getByText("01")).toBeInTheDocument();
  });
});
