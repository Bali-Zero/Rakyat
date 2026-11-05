// Kita Tax tab, 2026-11-03 owner asks: Coretax identity, two receipts then a
// disclosure, and the Personal tax / Company tax sections for tax clients.
// Synthetic fixtures only.
import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type {
  Client,
  ClientCompanyLink,
  ClientDocument,
  AttentionItem,
} from "@/lib/api/crm/crm.types";
import type { LKPMBatchItem, LKPMReceipt } from "@/lib/api/portal/portal.types";

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

const YEAR = new Date().getFullYear();
// 21 Mar 2026, noon WITA: personal SPT is in its last 10 days.
const NOW = new Date(Date.UTC(2026, 2, 21, 4, 0, 0));
const fmt = (d: string) => d;

const doc = (over: Partial<ClientDocument>): ClientDocument => ({
  id: Math.floor(Math.random() * 1e6),
  client_id: 7,
  document_type: "other",
  ...over,
});

function mkReport(over: Partial<LKPMBatchItem> = {}): LKPMBatchItem {
  return {
    id: 1,
    client_id: 7,
    company_name: "Acme Test PT",
    quarter: "Q1",
    year: YEAR,
    status: "draft",
    realized_total: 0,
    red_alerts: 0,
    yellow_alerts: 0,
    oss_submitted: false,
    client_approved: false,
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

function mkReceipt(n: number, over: Partial<LKPMReceipt> = {}): LKPMReceipt {
  return {
    id: n,
    lkpm_report_id: 1,
    nomor_laporan: `LAP-000${n}`,
    nomor_kegiatan_usaha: `NU-${n}`,
    kbli_code: "56101",
    kegiatan_usaha_desc: null,
    stage: "PRODUKSI",
    oss_status: "Disetujui",
    lokasi: null,
    tanggal_diterima: `2026-0${n}-10`,
    nama_perusahaan_oss: null,
    file_drive_url: `https://drive.example.test/r${n}`,
    file_name: `r${n}.pdf`,
    quarter: "Q1",
    year: YEAR,
    company_name: `Company ${n} PT`,
    ...over,
  };
}

const LINK = (id: number, name: string, primary = false): ClientCompanyLink =>
  ({
    link_id: id,
    company_id: id,
    company_name: name,
    company_type: "PT",
    role: "owner",
    is_primary: primary,
    status: "active",
  }) as ClientCompanyLink;

async function mount(props: {
  client?: Partial<Client> | null;
  documents?: ClientDocument[];
  companyLinks?: ClientCompanyLink[];
  attention?: AttentionItem[];
}) {
  const { TaxTab } = await import("./TaxTab");
  const client = props.client === null ? null : ({ ...props.client } as Client);
  return render(
    <TaxTab
      clientId={7}
      formatDate={fmt}
      client={client}
      companyLinks={props.companyLinks}
      documents={props.documents}
      attention={props.attention}
      taxConsultants={[]}
      now={NOW}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetClientHistory.mockResolvedValue({ items: [] });
  mockGetClientReceipts.mockResolvedValue({ items: [] });
});

describe("Tax identity — Coretax", () => {
  it("shows a 16-digit NPWP grouped, NIK-based for an individual, and no NIB", async () => {
    await mount({
      client: {
        npwp: "1234567890123456",
        client_type: "individual",
        nib: "9120107000111",
      },
    });
    expect(await screen.findByText("1234 5678 9012 3456")).toBeInTheDocument();
    expect(screen.getByText("NIK-based NPWP")).toBeInTheDocument();
    expect(screen.queryByText("9120107000111")).not.toBeInTheDocument();
    expect(screen.queryByText("NIB")).not.toBeInTheDocument();
  });

  it("a company 16-digit NPWP is grouped but not called NIK-based", async () => {
    await mount({
      client: { npwp: "1234567890123456", client_type: "company" },
    });
    expect(await screen.findByText("1234 5678 9012 3456")).toBeInTheDocument();
    expect(screen.queryByText("NIK-based NPWP")).not.toBeInTheDocument();
  });

  it("no access document: reads 'No Coretax access on file' and no EFIN", async () => {
    await mount({
      client: {},
      documents: [doc({ document_type: "passport" })],
    });
    expect(
      await screen.findByText("No Coretax access on file"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Coretax access on file"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("No EFIN on file")).toBeInTheDocument();
    expect(screen.queryByText("EFIN on file")).not.toBeInTheDocument();
  });

  it("access + EFIN documents: both on file, access carries its date", async () => {
    await mount({
      client: {},
      documents: [
        doc({ document_type: "coretax_access", issue_date: "2026-02-10" }),
        doc({ document_type: "efin" }),
      ],
    });
    const box = await screen.findByTestId("coretax-access");
    expect(box).toHaveTextContent("Coretax access on file · 2026-02-10");
    expect(within(box).getByText("EFIN on file")).toBeInTheDocument();
    expect(screen.queryByText("No Coretax access on file")).toBeNull();
  });
});

describe("OSS tanda terima — two entries then a disclosure", () => {
  it("5 receipts: 2 newest outside, 3 inside a closed details 'Show 3 more'", async () => {
    const user = userEvent.setup();
    mockGetClientReceipts.mockResolvedValue({
      items: [1, 2, 3, 4, 5].map((n) => mkReceipt(n)),
    });
    await mount({ client: null });
    const more = await screen.findByTestId("receipts-more");
    expect(more).not.toHaveAttribute("open");
    expect(within(more).getByText("Show 3 more")).toBeInTheDocument();
    // newest first: 5 and 4 are visible rows, 3/2/1 are in the disclosure
    expect(
      within(more)
        .getAllByText(/^LAP-/)
        .map((e) => e.textContent),
    ).toEqual(["LAP-0003", "LAP-0002", "LAP-0001"]);
    const outside = screen
      .getAllByText(/^LAP-/)
      .filter((e) => !more.contains(e))
      .map((e) => e.textContent);
    expect(outside).toEqual(["LAP-0005", "LAP-0004"]);
    // company name on each row
    expect(screen.getByText("Company 5 PT")).toBeInTheDocument();
    // keyboard-accessible native disclosure: summary toggles it
    await user.click(screen.getByText("Show 3 more"));
    expect(more).toHaveAttribute("open");
  });

  it("2 receipts or fewer: no disclosure", async () => {
    mockGetClientReceipts.mockResolvedValue({
      items: [mkReceipt(1), mkReceipt(2)],
    });
    await mount({ client: null });
    expect(await screen.findByText("LAP-0002")).toBeInTheDocument();
    expect(screen.getByText("LAP-0001")).toBeInTheDocument();
    expect(screen.queryByTestId("receipts-more")).not.toBeInTheDocument();
    expect(screen.queryByText(/Show \d+ more/)).not.toBeInTheDocument();
  });

  it("a receipt from another year is not counted toward the disclosure", async () => {
    mockGetClientReceipts.mockResolvedValue({
      items: [mkReceipt(1), mkReceipt(2), mkReceipt(3, { year: YEAR - 1 })],
    });
    await mount({ client: null });
    await screen.findByText("LAP-0002");
    expect(screen.queryByTestId("receipts-more")).not.toBeInTheDocument();
  });
});

describe("Personal tax / Company tax sections", () => {
  it("hidden for a client with no consultant and no LKPM history", async () => {
    await mount({ client: { tax_consultant: null } });
    await waitFor(() =>
      expect(screen.getByTestId("lkpm-row-Q1-empty")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("heading", { name: "Personal tax" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Company tax" })).toBeNull();
  });

  it("shown when a consultant is assigned; urgent personal, quiet company", async () => {
    await mount({ client: { tax_consultant: "dea.tax@example.test" } });
    expect(
      await screen.findByRole("heading", { name: "Personal tax" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Company tax" }),
    ).toBeInTheDocument();
    const personal = screen.getByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "urgent");
    expect(personal).toHaveTextContent("Closes 31 Mar · 10 days");
    expect(personal).toHaveTextContent("fiscal year 2025");
    const company = screen.getByTestId("spt-block-spt_company");
    expect(company).toHaveAttribute("data-state", "none");
    expect(company).toHaveTextContent("Next filing opens 31 Mar 2026");
  });

  it("shown for LKPM history alone (no consultant)", async () => {
    mockGetClientHistory.mockResolvedValue({ items: [mkReport()] });
    await mount({ client: { tax_consultant: null } });
    expect(
      await screen.findByRole("heading", { name: "Personal tax" }),
    ).toBeInTheDocument();
  });

  it("filed documents flip each section to Filed and list the history", async () => {
    await mount({
      client: { tax_consultant: "dea.tax@example.test" },
      documents: [
        doc({
          document_type: "spt_personal",
          fiscal_year: 2025,
          google_drive_file_url: "https://drive.example.test/p25",
        }),
        doc({ document_type: "spt_personal", fiscal_year: 2023 }),
        doc({ document_type: "spt", fiscal_year: 2025 }),
      ],
    });
    const personal = await screen.findByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "done");
    expect(within(personal).getByText("Filed")).toBeInTheDocument();
    const rows = within(personal).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("FY 2025"),
      expect.stringContaining("FY 2023"),
    ]);
    expect(
      within(personal).getByRole("link", { name: "Open SPT FY 2025" }),
    ).toHaveAttribute("href", "https://drive.example.test/p25");
    expect(screen.getByTestId("spt-block-spt_company")).toHaveAttribute(
      "data-state",
      "done",
    );
  });

  it("a personal filing does not mark the company SPT as filed", async () => {
    await mount({
      client: { tax_consultant: "dea.tax@example.test" },
      documents: [doc({ document_type: "spt_personal", fiscal_year: 2025 })],
    });
    expect(
      await screen.findByTestId("spt-block-spt_company"),
    ).not.toHaveAttribute("data-state", "done");
  });

  it("two companies: one Company tax block each, unattributed docs go to the primary", async () => {
    await mount({
      client: { tax_consultant: "dea.tax@example.test" },
      companyLinks: [LINK(1, "Alpha PT", true), LINK(2, "Beta PT")],
      documents: [doc({ document_type: "spt", fiscal_year: 2025 })],
    });
    const blocks = await screen.findAllByTestId("spt-block-spt_company");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toHaveTextContent("Alpha PT");
    expect(blocks[0]).toHaveAttribute("data-state", "done");
    expect(blocks[1]).toHaveTextContent("Beta PT");
    expect(blocks[1]).not.toHaveAttribute("data-state", "done");
  });

  it("one company: a single block with no company heading", async () => {
    await mount({
      client: { tax_consultant: "dea.tax@example.test" },
      companyLinks: [LINK(1, "Alpha PT", true)],
    });
    const blocks = await screen.findAllByTestId("spt-block-spt_company");
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).not.toHaveTextContent("Alpha PT");
  });
});

const item = (over: Partial<AttentionItem>): AttentionItem => ({
  kind: "spt_personal",
  subject: "client",
  subject_id: 7,
  label: "SPT personal 2025",
  state: "urgent",
  subject_name: null,
  code: null,
  date: "2026-03-31",
  opens_on: null,
  days: 3,
  note: null,
  document_id: null,
  ...over,
});

describe("attention engine vs the local helper", () => {
  const client = { tax_consultant: "dea.tax@example.test" };

  it("an attention item wins over the helper (state, days and date are the engine's)", async () => {
    // The helper would say urgent / 10 days at NOW; the engine says overdue.
    await mount({
      client,
      attention: [item({ state: "overdue", days: -4, date: "2026-03-31" })],
    });
    const personal = await screen.findByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "overdue");
    expect(personal).toHaveTextContent("Overdue by 4 days · 31 Mar");
    expect(personal).not.toHaveTextContent("Closes 31 Mar");
  });

  it("an engine 'done' item shows Filed even with no document in the list", async () => {
    await mount({ client, attention: [item({ state: "done" })] });
    const personal = await screen.findByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "done");
    expect(within(personal).getByText("Filed")).toBeInTheDocument();
  });

  it("no attention: the helper answers", async () => {
    await mount({ client });
    const personal = await screen.findByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "urgent");
    expect(personal).toHaveTextContent("Closes 31 Mar · 10 days");
  });

  it("attention without an SPT item for the kind falls back to the helper", async () => {
    await mount({
      client,
      attention: [item({ kind: "permit", state: "overdue", label: "KITAS" })],
    });
    expect(await screen.findByTestId("spt-block-spt_personal")).toHaveAttribute(
      "data-state",
      "urgent",
    );
  });

  it("company blocks match engine items by subject_id", async () => {
    await mount({
      client,
      companyLinks: [LINK(1, "Alpha PT", true), LINK(2, "Beta PT")],
      attention: [
        item({
          kind: "spt_company",
          subject: "company",
          subject_id: 2,
          label: "SPT company 2025",
          state: "urgent",
          date: "2026-04-30",
          days: 5,
        }),
      ],
    });
    const blocks = await screen.findAllByTestId("spt-block-spt_company");
    // Alpha has no engine item: helper (21 Mar -> 40 days, quiet).
    expect(blocks[0]).toHaveAttribute("data-state", "none");
    // Beta takes the engine's urgent line.
    expect(blocks[1]).toHaveAttribute("data-state", "urgent");
    expect(blocks[1]).toHaveTextContent("Closes 30 Apr · 5 days");
  });
});

describe("after the SPT window with nothing filed", () => {
  // 3 Nov 2026: both 2025 windows are long closed.
  const LATE = new Date(Date.UTC(2026, 10, 3, 4, 0, 0));
  async function mountLate(documents: ClientDocument[] = []) {
    const { TaxTab } = await import("./TaxTab");
    return render(
      <TaxTab
        clientId={7}
        formatDate={fmt}
        client={{ tax_consultant: "dea.tax@example.test" } as Client}
        documents={documents}
        taxConsultants={[]}
        now={LATE}
      />,
    );
  }

  it("points at the next filing and lists the unfiled year quietly", async () => {
    await mountLate();
    const company = await screen.findByTestId("spt-block-spt_company");
    expect(company).toHaveTextContent("Next: SPT fiscal year 2026");
    expect(company).toHaveTextContent("opens 31 Mar 2027");
    expect(company).not.toHaveTextContent("fiscal year 2025 ");
    const missing = within(company).getByTestId("spt-missing-year");
    expect(missing).toHaveTextContent("FY 2025");
    expect(missing).toHaveTextContent("no SPT on file");
    const personal = screen.getByTestId("spt-block-spt_personal");
    expect(personal).toHaveTextContent("opens 1 Mar 2027");
    // quiet: no warning colour on the status line
    const status = within(company).getByTestId("spt-status");
    expect(status.style.color).toBe("var(--tx-secondary)");
  });

  it("a filed year shows Filed and no missing-year row", async () => {
    await mountLate([
      doc({ document_type: "spt_personal", fiscal_year: 2025 }),
    ]);
    const personal = await screen.findByTestId("spt-block-spt_personal");
    expect(personal).toHaveAttribute("data-state", "done");
    expect(within(personal).queryByTestId("spt-missing-year")).toBeNull();
    // the company side is still unfiled
    expect(
      within(screen.getByTestId("spt-block-spt_company")).getByTestId(
        "spt-missing-year",
      ),
    ).toBeInTheDocument();
  });
});
