/** Company ledger behaviour — synthetic fixtures only, no client data. */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompanyTab } from "./CompanyTab";
import { clearCompanyDetailCache } from "./company/companyDetailCache";
import type { ClientCompanyLink, ClientProfile } from "@/lib/api/crm/crm.types";

const {
  mockGetClientCompanies,
  mockGetCompany,
  mockSearchCompanyByName,
  mockPost,
  mockGetClientRecap,
  mockGetCompanyDriveSources,
} = vi.hoisted(() => ({
  mockGetClientCompanies: vi.fn(),
  mockGetCompany: vi.fn().mockResolvedValue({ associates: [], documents: [] }),
  mockSearchCompanyByName: vi.fn().mockResolvedValue(null),
  mockPost: vi.fn(),
  mockGetClientRecap: vi.fn(),
  mockGetCompanyDriveSources: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getClientCompanies: mockGetClientCompanies,
      getCompany: mockGetCompany,
      searchCompanyByName: mockSearchCompanyByName,
      getCompanyDocuments: vi.fn().mockResolvedValue([]),
      getClientRecap: mockGetClientRecap,
      getCompanyDriveSources: mockGetCompanyDriveSources,
    },
    post: mockPost,
  },
}));

vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

// Stubbed so the "no per-tab AI card" assertions can fail: the real card
// carries no test id, so without this a restored card would stay invisible.
vi.mock("./AiSummaryCard", () => ({
  AiSummaryCard: () => <div data-testid="AiSummaryCard" />,
}));

vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

const CLIENT = {
  id: 412,
  full_name: "Client A",
  company_name: undefined,
} as unknown as ClientProfile["client"];

function link(overrides: Partial<ClientCompanyLink> = {}): ClientCompanyLink {
  return {
    link_id: 1,
    company_id: 90,
    company_name: "PT Synthetic Studio",
    company_type: "PT",
    role: "director",
    is_primary: true,
    status: "active",
    company_status: "active",
    nib: "9900000010000",
    npwp_company: "84.221.907.4-901.000",
    kbli_code: "70209",
    kbli_description: "Other Management Consulting Activities",
    registered_address: "Jl. Synthetic No. 1",
    city: "Denpasar",
    province: "Bali",
    ...overrides,
  } as ClientCompanyLink;
}

const renderTab = () =>
  render(
    <CompanyTab
      clientId={412}
      client={CLIENT}
      documents={[]}
      formatDate={(d: string) => `formatted:${d}`}
      onRefresh={() => Promise.resolve()}
    />,
  );

// Stand-in for GET /api/kbli/resolve (the real resolver is covered against the
// real dataset in src/lib/kbli-resolve.test.ts). Codes absent from NAVIGATOR are
// "not in KBLI 2025"; CROSSWALK is the BPS 2020->2025 successor table.
const NAVIGATOR: Record<string, string> = {
  "70209": "Navigator Title For 70209",
  "62011": "Navigator Title For 62011",
  "55203": "Navigator Title For 55203",
  "55209": "Navigator Title For 55209",
};
const CROSSWALK: Record<string, string[]> = { "55199": ["55209"] };
const mockFetch = vi.fn();
function fakeResolve(url: string) {
  const codes = decodeURIComponent(url.split("codes=")[1] ?? "").split(",");
  const resolutions: Record<string, unknown> = {};
  for (const c of codes) {
    resolutions[c] = NAVIGATOR[c]
      ? { inNavigator: true, title: NAVIGATOR[c], successors: [] }
      : {
          inNavigator: false,
          successors: (CROSSWALK[c] ?? []).map((n) => ({
            code: n,
            title: NAVIGATOR[n],
          })),
        };
  }
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ resolutions }),
  });
}

afterEach(() => vi.clearAllMocks());
beforeEach(() => {
  mockFetch.mockReset();
  mockFetch.mockImplementation((url: string) => fakeResolve(url));
  vi.stubGlobal("fetch", mockFetch);
  clearCompanyDetailCache();
  mockGetClientRecap.mockReset();
  mockGetClientRecap.mockRejectedValue(new Error("no recap"));
  mockGetCompany.mockResolvedValue({ associates: [], documents: [] });
  mockSearchCompanyByName.mockResolvedValue(null);
  mockGetCompanyDriveSources.mockImplementation(async (companyId: number) => ({
    company_id: companyId,
    sources: [
      {
        url: `https://drive.google.com/drive/folders/synthetic_folder_${companyId}`,
        kinds: ["primary"],
      },
    ],
    lineage_status: "none",
    merged_into_company_id: null,
    historical_record_ids: [],
  }));
});

describe("CompanyTab — r19 kv-grid ledger", () => {
  it("empty state: no company, no company name, no docs — r19 EmptyState sentence, no dashed box, no AiSummaryCard", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([]);
    render(
      <CompanyTab
        clientId={412}
        client={{ ...CLIENT, company_name: undefined }}
        documents={[]}
        formatDate={(d: string) => d}
        onRefresh={() => Promise.resolve()}
      />,
    );
    expect(
      await screen.findByText("No company linked yet."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add company" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("AiSummaryCard")).not.toBeInTheDocument();
  });

  it("GUILT: company_type 'PT' renders through companyType.ts as 'PT (local / PMDN)', not the raw string (PR 6827 gap)", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_type: "PT" }),
    ]);
    renderTab();
    expect(await screen.findByText("PT (local / PMDN)")).toBeInTheDocument();
    // The raw, unmapped string must not appear as its own cell value.
    expect(screen.queryByText("PT", { exact: true })).not.toBeInTheDocument();
  });

  it("GUILT: a legacy alias (PMDN) normalises to the same canonical label", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_type: "PMDN" }),
    ]);
    renderTab();
    expect(await screen.findByText("PT (local / PMDN)")).toBeInTheDocument();
  });

  it("company identity kv-grid shows legal name, NIB, NPWP, KBLI and address from real fields", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    expect(await screen.findByText("PT Synthetic Studio")).toBeInTheDocument();
    expect(screen.getByText("9900000010000")).toBeInTheDocument();
    expect(screen.getByText("84.221.907.4-901.000")).toBeInTheDocument();
    // "70209" and its description appear twice by design: once as the
    // at-a-glance identity kv-item, once as the full row in Business
    // activities (parity items 35-37 — both old facts kept, not merged).
    expect(screen.getAllByText("70209").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText("Other Management Consulting Activities").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Jl. Synthetic No. 1")).toBeInTheDocument();
    expect(screen.queryByTestId("AiSummaryCard")).not.toBeInTheDocument();
  });

  it("identity glosses survive the restyle: NIB and NPWP expansions, and the Indonesian entity-type expansion on a legacy spelling", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_type: "PT_PMA" }),
    ]);
    renderTab();
    expect(
      await screen.findByText("Nomor Induk Berusaha · OSS registered"),
    ).toBeInTheDocument();
    expect(screen.getByText("Tax Identification Number")).toBeInTheDocument();
    expect(screen.getByText("PT PMA")).toBeInTheDocument();
    expect(screen.getByText("Penanaman Modal Asing")).toBeInTheDocument();
  });

  it("INNOCENCE: an entity type with no known expansion shows no gloss", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_type: "PT" }),
    ]);
    renderTab();
    await screen.findByText("PT (local / PMDN)");
    expect(screen.queryByText("Penanaman Modal Asing")).not.toBeInTheDocument();
  });

  it("INNOCENCE: 'Edit company' icon button is reachable when a company is linked", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    expect(
      await screen.findByRole("button", { name: "Edit company" }),
    ).toBeInTheDocument();
  });

  it("GUILT: 'Edit company' button is absent when there is no linked company_id", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([]);
    render(
      <CompanyTab
        clientId={412}
        client={{ ...CLIENT, company_name: "Unlinked Co" }}
        documents={[]}
        formatDate={(d: string) => d}
        onRefresh={() => Promise.resolve()}
      />,
    );
    await screen.findByText("Unlinked Co");
    expect(
      screen.queryByRole("button", { name: "Edit company" }),
    ).not.toBeInTheDocument();
  });

  it("the NIB copy action writes to the clipboard and confirms", async () => {
    // fireEvent, not userEvent: userEvent.setup() installs its OWN clipboard
    // stub on navigator.clipboard, shadowing the vi.fn() from test/setup.tsx
    // that this assertion needs to inspect.
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    const copyBtn = await screen.findByRole("button", { name: "Copy NIB" });
    fireEvent.click(copyBtn);
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "9900000010000",
      ),
    );
  });

  it("GUILT (gap closed): company_status 'dissolved' now renders its real word — the old chip rendered nothing for this value", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_status: "dissolved" }),
    ]);
    renderTab();
    expect(await screen.findByText("Dissolved")).toBeInTheDocument();
  });

  it("shareholders & officers: role and ownership stay visible without a colour-coded well", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        {
          client_name: "Shareholder One",
          role: "director",
          ownership_percentage: 60,
          shares_count: 600,
        },
        {
          client_name: "Shareholder Two",
          role: "commissioner",
          ownership_percentage: 40,
          shares_count: 400,
        },
      ],
      documents: [],
    });
    renderTab();
    expect(await screen.findByText("Shareholder One")).toBeInTheDocument();
    expect(screen.getByText("director")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText("Shareholder Two")).toBeInTheDocument();
    expect(screen.getByText("40%")).toBeInTheDocument();
  });

  it("GUILT: OCR shareholders' ownership is a share of the company's shares_count, not of the OCR'd rows", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        shares_count: 10000,
        custom_fields: {
          shareholders: [
            { name: "Holder A", role: "Director", shares: 4000 },
            { name: "Holder B", role: "Commissioner", shares: 4000 },
          ],
        },
      }),
    ]);
    renderTab();
    expect(await screen.findByText("Holder A")).toBeInTheDocument();
    expect(screen.getAllByText("40%")).toHaveLength(2);
    // 50% is what the rows' own sum (8000) would give.
    expect(screen.queryByText("50%")).not.toBeInTheDocument();
  });

  it("INNOCENCE: with no shares_count the OCR rows' own sum is the denominator", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        custom_fields: {
          shareholders: [
            { name: "Holder A", shares: 4000 },
            { name: "Holder B", shares: 4000 },
          ],
        },
      }),
    ]);
    renderTab();
    expect(await screen.findByText("Holder A")).toBeInTheDocument();
    expect(screen.getAllByText("50%")).toHaveLength(2);
  });

  it("boundary: the OCR list replaces the linked fezotijene only when it is LONGER (1 vs 1 keeps the associate, 2 vs 1 switches)", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        custom_fields: { shareholders: [{ name: "Ocr Only", shares: 100 }] },
      }),
    ]);
    const first = renderTab();
    expect(await screen.findByText("Client A")).toBeInTheDocument();
    expect(screen.queryByText("Ocr Only")).not.toBeInTheDocument();
    first.unmount();

    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        custom_fields: {
          shareholders: [
            { name: "Ocr Only", shares: 100 },
            { name: "Ocr Second", shares: 100 },
          ],
        },
      }),
    ]);
    renderTab();
    expect(await screen.findByText("Ocr Only")).toBeInTheDocument();
    expect(screen.queryByText("Client A")).not.toBeInTheDocument();
  });

  it("legal timeline: incorporation date renders only through the formatDate prop, never a raw locale format", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        akta_pendirian_no: "12",
        akta_pendirian_date: "2019-03-04",
        sk_menhumkam_no: "AHU-000",
      }),
    ]);
    renderTab();
    // Renders twice by design: the Capital & shares kv-item and the Legal
    // timeline row each show the same real incorporation date independently
    // (same duplication the old KeyNumbersColumn/LegalTimeline pair had).
    const dates = await screen.findAllByText("formatted:2019-03-04");
    expect(dates.length).toBeGreaterThan(0);
    // The old component's own `toLocaleDateString(... {month:"long"})` would
    // have produced "March" — that must never appear, since formatDate is the
    // only sanctioned date renderer (SSR hydration law).
    expect(screen.queryByText(/March/)).not.toBeInTheDocument();
  });

  it("GUILT: the NIB/OSS timeline entry shows no fabricated year — the old heuristic (skDate year + 1) is dropped, not replaced by a guess", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        nib: "9900000010000",
        sk_menhumkam_no: "AHU-000",
        sk_menhumkam_date: "2020-01-01",
        akta_pendirian_no: undefined,
        akta_pendirian_date: undefined,
      }),
    ]);
    renderTab();
    expect(
      await screen.findByText("NIB Issued & OSS Platform Verified"),
    ).toBeInTheDocument();
    // "2021" would be the old fabricated (skDate year + 1) guess.
    expect(screen.queryByText("2021")).not.toBeInTheDocument();
  });

  it("document vault renders its fixed slots when a company is linked", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    expect(await screen.findByText("Akta Pendirian")).toBeInTheDocument();
    expect(screen.getByText("NPWP Perusahaan")).toBeInTheDocument();
    expect(screen.getByText("Rekening Koran")).toBeInTheDocument();
  });
});

describe("CompanyTab — more than one linked company", () => {
  it("GUILT: the primary link is shown even when the API returns it second", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        link_id: 11,
        company_id: 91,
        company_name: "PT Synthetic Pool",
        is_primary: false,
      }),
      link({
        link_id: 12,
        company_id: 92,
        company_name: "PT Synthetic Own",
        is_primary: true,
      }),
    ]);
    renderTab();
    expect(await screen.findByText("PT Synthetic Own")).toBeInTheDocument();
    await waitFor(() => expect(mockGetCompany).toHaveBeenCalledWith(92));
    expect(mockGetCompany).not.toHaveBeenCalledWith(91);
  });

  it("GUILT: the other links are listed, so a second PT is not silently hidden", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        link_id: 11,
        company_id: 91,
        company_name: "PT Synthetic Pool",
        role: "shareholder",
        is_primary: false,
      }),
      link({
        link_id: 12,
        company_id: 92,
        company_name: "PT Synthetic Own",
        is_primary: true,
      }),
    ]);
    renderTab();
    const list = await screen.findByRole("list", {
      name: "Other linked companies",
    });
    expect(list).toHaveTextContent("PT Synthetic Pool");
    expect(list).toHaveTextContent("shareholder");
    expect(list).not.toHaveTextContent("PT Synthetic Own");
  });

  it("boundary: with no link flagged primary, the first link is kept (old behaviour) and the rest are listed", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        link_id: 21,
        company_id: 93,
        company_name: "PT Synthetic First",
        is_primary: false,
      }),
      link({
        link_id: 22,
        company_id: 94,
        company_name: "PT Synthetic Second",
        is_primary: false,
      }),
    ]);
    renderTab();
    expect(await screen.findByText("PT Synthetic First")).toBeInTheDocument();
    const list = await screen.findByRole("list", {
      name: "Other linked companies",
    });
    expect(list).toHaveTextContent("PT Synthetic Second");
  });

  it("INNOCENCE: a single link renders no 'other linked companies' list", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    await screen.findByText("PT Synthetic Studio");
    expect(
      screen.queryByRole("list", { name: "Other linked companies" }),
    ).not.toBeInTheDocument();
  });
});

describe("CompanyTab — interactive people, KBLI codes and legal timeline", () => {
  const associate = (over: Record<string, unknown>) => ({
    role: "director",
    ownership_percentage: 50,
    shares_count: 500,
    ...over,
  });

  it("the person's name is the primary text of the row: larger, heavier, heading face, linked or not", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        associate({ client_id: 77, client_name: "Shareholder One" }),
        associate({ client_name: "Shareholder Two", role: "commissioner" }),
      ],
      documents: [],
    });
    renderTab();
    await screen.findByText("Shareholder One");
    const names = screen.getAllByTestId("person-name");
    expect(names).toHaveLength(2);
    for (const n of names) {
      expect(n.className).toContain("text-[15px]");
      expect(n.className).toContain("sm:text-[16px]");
      expect(n.className).toContain("truncate");
      expect(n.style.fontWeight).toBe("600");
      expect(n.style.fontFamily).toContain("--font-serif");
    }
    const role = screen.getByText("commissioner");
    expect(role.className).not.toContain("text-[15px]");
  });

  it("an associate with a client_id links to that client card", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        associate({ client_id: 77, client_name: "Shareholder One" }),
        associate({ client_name: "Shareholder Two", role: "commissioner" }),
      ],
      documents: [],
    });
    renderTab();
    const a = await screen.findByRole("link", { name: "Shareholder One" });
    expect(a).toHaveAttribute("href", "/clients/77");
    // GUILT: a non-client is plain text, no link.
    expect(screen.getByText("Shareholder Two")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Shareholder Two" }),
    ).not.toBeInTheDocument();
  });

  it("the current client is never linked to itself", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [associate({ client_id: 412, client_name: "Client A" })],
      documents: [],
    });
    renderTab();
    expect(await screen.findByText("Client A")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Client A" }),
    ).not.toBeInTheDocument();
  });

  it("an OCR row links when its name matches an associate (case-insensitive, trimmed); a near-miss does not", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        custom_fields: {
          shareholders: [
            { name: "  shareholder ONE ", shares: 100 },
            { name: "Shareholder One Jr", shares: 100 },
            { name: "Unknown Person", shares: 100 },
          ],
        },
      }),
    ]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        associate({ client_id: 77, client_name: "Shareholder One" }),
      ],
      documents: [],
    });
    renderTab();
    const matched = await screen.findByRole("link", {
      name: /shareholder ONE/i,
    });
    expect(matched).toHaveAttribute("href", "/clients/77");
    // The link is discoverable: arrow icon after the name, avatar inside it.
    expect(matched.querySelector("svg")).not.toBeNull();
    expect(matched.textContent).toContain("SO");
    expect(matched.className).toContain("focus-visible:ring-2");
    expect(screen.getAllByRole("link", { name: /shareholder/i })).toHaveLength(
      1,
    );
    expect(screen.getByText("Unknown Person")).toBeInTheDocument();
  });

  it("each KBLI 2025 code is an anchor to the navigator, opening in a new tab", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: "70209, 62011" }),
    ]);
    renderTab();
    const a = await screen.findByRole("link", { name: /70209/ });
    expect(a).toHaveAttribute("href", "/kbli/70209");
    expect(a).toHaveAttribute("target", "_blank");
    expect(a.getAttribute("rel")).toContain("noopener");
    expect(screen.getByRole("link", { name: /62011/ })).toHaveAttribute(
      "href",
      "/kbli/62011",
    );
  });

  it("the legal timeline lists milestones newest first, undated ones after, and marks the latest", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        akta_pendirian_no: "12",
        akta_pendirian_date: "2019-03-04",
        sk_menhumkam_no: "AHU-000",
        sk_menhumkam_date: "2019-04-10",
        akta_perubahan_no: "30",
        akta_perubahan_date: "2022-08-01",
      }),
    ]);
    renderTab();
    const list = await screen.findByRole("list", { name: "Legal milestones" });
    const items = Array.from(list.querySelectorAll(":scope > li"));
    const text = items.map((li) => li.textContent ?? "");
    const idx = (needle: string) => text.findIndex((t) => t.includes(needle));
    expect(idx("formatted:2022-08-01")).toBe(0);
    expect(idx("formatted:2019-04-10")).toBeLessThan(
      idx("formatted:2019-03-04"),
    );
    expect(idx("formatted:2019-03-04")).toBeLessThan(idx("NIB 9900"));
    expect(idx("NIB 9900")).toBeLessThan(text.length);
    expect(items[0].getAttribute("data-latest")).toBe("true");
    expect(items[1].getAttribute("data-latest")).toBeNull();
    expect(screen.getByText("Deed of establishment")).toBeInTheDocument();
    expect(screen.getByText("SK Menkumham")).toBeInTheDocument();
    expect(screen.getByText("Amendment deed")).toBeInTheDocument();
  });

  it("the documents count opens the Documents tab through the handler; without a handler it is plain text", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [],
      documents: [
        {
          id: 1,
          uuid: "u",
          document_type: "nib",
          status: "active",
          google_drive_file_url: "https://example.test/f",
          created_at: "",
        },
      ],
    });
    const onOpen = vi.fn();
    render(
      <CompanyTab
        clientId={412}
        client={CLIENT}
        documents={[]}
        formatDate={(d: string) => d}
        onRefresh={() => Promise.resolve()}
        onOpenDocuments={onOpen}
      />,
    );
    const btn = await screen.findByRole("button", {
      name: /1 document on file/,
    });
    fireEvent.click(btn);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("CompanyTab — owner round 2026-11-03 (Established first, one-line capital, English KBLI, badge)", () => {
  it("the first identity tile is Established with the AHU number(s) and age under it, before NIB and NPWP", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        akta_pendirian_no: "12",
        akta_pendirian_date: "2019-03-04",
        sk_menhumkam_no: "AHU-0000001.AH.01.01 / AHU-0000002.AH.01.02",
      }),
    ]);
    renderTab();
    const label = await screen.findByText("Established");
    const tile = label.parentElement!;
    expect(tile.textContent).toContain("formatted:2019-03-04");
    expect(tile.textContent).toContain("AHU-0000001.AH.01.01");
    expect(tile.textContent).toContain("AHU-0000002.AH.01.02");
    expect(tile.textContent).toMatch(/\d+y \d+m old/);
    const labels = Array.from(tile.parentElement!.children).map(
      (t) => t.firstElementChild?.textContent,
    );
    expect(labels.indexOf("Established")).toBe(0);
    expect(labels.indexOf("NIB")).toBe(1);
    expect(labels.indexOf("NPWP")).toBe(2);
  });

  it("GUILT: Capital & shares no longer carries an Incorporation date item; INNOCENCE: its other three stay", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        akta_pendirian_no: "12",
        akta_pendirian_date: "2019-03-04",
        akta_perubahan_no: "30",
        akta_perubahan_date: "2022-08-01",
        shares_count: 1000,
        share_nominal_value: 1000000,
      }),
    ]);
    renderTab();
    expect(await screen.findByText("Share structure")).toBeInTheDocument();
    expect(screen.queryByText("Incorporation date")).not.toBeInTheDocument();
    expect(screen.getByText("Last amendment")).toBeInTheDocument();
    const grid = screen
      .getByText("Share structure")
      .closest("div.grid") as HTMLElement;
    expect(grid.className).toContain("lg:grid-cols-3");
    expect(grid.children).toHaveLength(3);
  });

  it("a 2025 code shows the navigator's own title (stored description as the secondary line) and links to /kbli/<code>", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        kbli_code: "55203",
        kbli_description: "Penyewaan Vila",
      }),
    ]);
    renderTab();
    expect(
      await screen.findByText("Navigator Title For 55203"),
    ).toBeInTheDocument();
    expect(screen.getByText("Penyewaan Vila")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /55203/ })).toHaveAttribute(
      "href",
      "/kbli/55203",
    );
    expect(screen.queryByTestId("kbli-2020-flag")).not.toBeInTheDocument();
  });

  it("the round KBLI Navigator badge sits next to the Business activities title and opens the navigator in a new tab", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    const img = await screen.findByAltText("KBLI Navigator");
    expect(img.getAttribute("src")).toContain("kbli-navigator-badge.webp");
    const a = img.closest("a")!;
    expect(a).toHaveAttribute("href", "/kbli");
    expect(a).toHaveAttribute("target", "_blank");
    expect(a.getAttribute("rel")).toContain("noopener");
    expect(a.parentElement!.textContent).toContain("Business activities");
  });

  it("INNOCENCE: with no KBLI code there is no badge", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: undefined, kbli_description: undefined }),
    ]);
    renderTab();
    await screen.findByText("PT Synthetic Studio");
    expect(screen.queryByAltText("KBLI Navigator")).not.toBeInTheDocument();
  });
});

describe("CompanyTab — AI summary slot beside the company name", () => {
  it("asks for the company scope with this company's id and shows the text in the identity block", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetClientRecap.mockResolvedValueOnce({
      scope: "company",
      text: "Synthetic company summary.",
      generated_at: "2026-11-03T09:00:00Z",
      model: null,
      stale: false,
    });
    renderTab();
    const text = await screen.findByText("Synthetic company summary.");
    expect(mockGetClientRecap).toHaveBeenCalledWith(412, "company", 90);
    expect(
      screen.getByText("PT Synthetic Studio").parentElement!.parentElement,
    ).toContainElement(text);
  });

  it("INNOCENCE: nothing renders when there is no recap", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    await screen.findByText("PT Synthetic Studio");
    await waitFor(() =>
      expect(screen.queryByTestId("ai-recap-skeleton")).not.toBeInTheDocument(),
    );
    expect(screen.queryByLabelText("AI summary")).not.toBeInTheDocument();
  });

  it("GUILT: a 2020-only code is flagged, is not a link, and keeps its legacy English title; INNOCENCE: a 2025 code beside it is not flagged", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({
        kbli_code: "55193, 70209",
        kbli_description: "Vila; Konsultan",
      }),
    ]);
    renderTab();
    expect(await screen.findByText("Villa")).toBeInTheDocument();
    const flags = screen.getAllByTestId("kbli-2020-flag");
    expect(flags).toHaveLength(1);
    expect(flags[0].textContent).toContain(
      "KBLI 2020 · not yet migrated to KBLI 2025",
    );
    expect(flags[0].textContent).not.toContain("→ 2025");
    expect(
      screen.queryByRole("link", { name: /55193/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /70209/ })).toHaveAttribute(
      "href",
      "/kbli/70209",
    );
  });

  it("a concordance suggestion appears only when the crosswalk has the code, and links to the 2025 page", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: "55199, 55193", kbli_description: "A; B" }),
    ]);
    renderTab();
    await screen.findAllByTestId("kbli-2020-flag");
    const [withMap, withoutMap] = screen.getAllByTestId("kbli-2020-flag");
    expect(withMap.textContent).toContain(
      "→ 2025: 55209 Navigator Title For 55209",
    );
    expect(withMap.querySelector("a")).toHaveAttribute("href", "/kbli/55209");
    expect(withoutMap.textContent).not.toContain("→ 2025");
  });

  it("GUILT: a code with no stored title and no table entry shows the code alone, unlinked, with no invented title", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: "55115", kbli_description: "" }),
    ]);
    renderTab();
    expect(await screen.findByTestId("kbli-2020-flag")).toBeInTheDocument();
    expect(screen.getByText("KBLI 55115")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /55115/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("One-star hotel")).not.toBeInTheDocument();
  });

  it("INNOCENCE: when the resolver is unreachable nothing is flagged and no row links to a page that may not exist", async () => {
    mockFetch.mockRejectedValue(new Error("offline"));
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: "70209", kbli_description: "Konsultan" }),
    ]);
    renderTab();
    expect(await screen.findByText("Konsultan")).toBeInTheDocument();
    expect(screen.queryByTestId("kbli-2020-flag")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /70209/ }),
    ).not.toBeInTheDocument();
  });
});

describe("CompanyTab — no serial round trips", () => {
  const withLinks = () =>
    render(
      <CompanyTab
        clientId={412}
        client={CLIENT}
        documents={[]}
        formatDate={(d: string) => d}
        onRefresh={() => Promise.resolve()}
        companyLinks={[link()]}
      />,
    );

  it("with company_links from the profile the identity renders with no getClientCompanies call and ONE request (the detail); re-opening issues none", async () => {
    mockGetCompany.mockResolvedValue({
      associates: [
        {
          client_id: 77,
          client_name: "Shareholder One",
          role: "director",
          ownership_percentage: 100,
          shares_count: 1000,
        },
      ],
      documents: [],
    });
    const first = withLinks();
    expect(await screen.findByText("PT Synthetic Studio")).toBeInTheDocument();
    await screen.findByText("Shareholder One");
    expect(mockGetClientCompanies).not.toHaveBeenCalled();
    expect(mockGetCompany).toHaveBeenCalledTimes(1);
    expect(mockGetCompany).toHaveBeenCalledWith(90);
    first.unmount();

    withLinks();
    expect(await screen.findByText("Shareholder One")).toBeInTheDocument();
    expect(mockGetClientCompanies).not.toHaveBeenCalled();
    expect(mockGetCompany).toHaveBeenCalledTimes(1);
  });

  it("GUILT: without company_links the old path still asks for the links; INNOCENCE: a cache is per company, so another company refetches", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    await screen.findByText("PT Synthetic Studio");
    expect(mockGetClientCompanies).toHaveBeenCalledTimes(1);
    cleanup();
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ company_id: 91, company_name: "PT Other Synthetic" }),
    ]);
    renderTab();
    await screen.findByText("PT Other Synthetic");
    await waitFor(() => expect(mockGetCompany).toHaveBeenCalledWith(91));
  });

  it("a mutation from the tab (Sync Drive that adds a file) drops the cached detail: the tab refetches now and a later open shows the fresh copy", async () => {
    mockGetCompany
      .mockResolvedValueOnce({
        associates: [
          {
            client_id: 77,
            client_name: "Holder Before",
            role: "director",
            ownership_percentage: 100,
            shares_count: 10,
          },
        ],
        documents: [],
      })
      .mockResolvedValueOnce({
        associates: [
          {
            client_id: 77,
            client_name: "Holder After",
            role: "director",
            ownership_percentage: 100,
            shares_count: 10,
          },
        ],
        documents: [],
      });
    // After a mutation the tab asks the server for the links again.
    mockGetClientCompanies.mockResolvedValue([link()]);
    mockPost.mockResolvedValueOnce({
      added: 1,
      skipped: 0,
      total_in_folder: 1,
    });
    const first = withLinks();
    await screen.findByText("Holder Before");
    fireEvent.click(await screen.findByRole("button", { name: /sync drive/i }));
    expect(await screen.findByText("Holder After")).toBeInTheDocument();
    expect(mockGetCompany).toHaveBeenCalledTimes(2);
    first.unmount();
    withLinks();
    expect(await screen.findByText("Holder After")).toBeInTheDocument();
    expect(mockGetCompany).toHaveBeenCalledTimes(2);
  });

  it("the client card's invalidate (clearCompanyDetailCache) makes the next open refetch; without it the open is served from the cache", async () => {
    mockGetCompany.mockResolvedValue({ associates: [], documents: [] });
    withLinks().unmount();
    await waitFor(() => expect(mockGetCompany).toHaveBeenCalledTimes(1));
    withLinks().unmount();
    expect(mockGetCompany).toHaveBeenCalledTimes(1);
    clearCompanyDetailCache();
    withLinks();
    await waitFor(() => expect(mockGetCompany).toHaveBeenCalledTimes(2));
  });
});

describe("CompanyTab — section numbers follow what renders; 390px layout", () => {
  const sections = (container: HTMLElement) =>
    Array.from(container.querySelectorAll("section")).map((s) => {
      const head = s.firstElementChild as HTMLElement;
      const n = head.querySelector("span")?.textContent;
      const title = head.querySelector("h2")?.textContent ?? "";
      return `${n} ${title.replace(/\s+/g, " ").trim()}`;
    });

  it("GUILT: with no capital facts the numbers run 01, 02, 03 — no gap where Capital & shares is hidden", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    const { container } = renderTab();
    await screen.findByText("PT Synthetic Studio");
    const heads = sections(container);
    expect(heads[0]).toMatch(/^01 Company identity/);
    expect(heads.length).toBeGreaterThanOrEqual(2);
    expect(heads.some((h) => h.includes("Capital & shares"))).toBe(false);
    expect(heads.map((h) => h.slice(0, 2))).toEqual(
      heads.map((_, i) => String(i + 1).padStart(2, "0")),
    );
  });

  it("INNOCENCE: with capital, shareholders and KBLI present the numbers run 01 to 04 in order", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ shares_count: 1000, share_nominal_value: 1000000 }),
    ]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        {
          role: "director",
          client_name: "Shareholder One",
          ownership_percentage: 100,
          shares_count: 1000,
        },
      ],
      documents: [],
    });
    const { container } = renderTab();
    await screen.findByText("Shareholder One");
    const heads = sections(container);
    expect(heads.slice(0, 4).map((h) => h.slice(0, 2))).toEqual([
      "01",
      "02",
      "03",
      "04",
    ]);
    expect(heads[1]).toContain("Capital & shares");
    expect(heads[2]).toContain("Shareholders");
    expect(heads[3]).toContain("Business activities");
  });

  it("below sm a shareholder row stacks: the name spans the row, role/shares/ownership are a labelled meta line, and the column head is hidden", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    mockGetCompany.mockResolvedValueOnce({
      associates: [
        {
          role: "director",
          client_name: "Shareholder One",
          ownership_percentage: 100,
          shares_count: 1000,
        },
      ],
      documents: [],
    });
    renderTab();
    const name = (await screen.findByTestId("person-name")).closest(
      "div.min-w-0",
    )!;
    expect(name.className).toContain("max-sm:col-span-3");
    const row = name.parentElement!;
    expect(row.className).toContain("max-sm:grid-cols-3");
    const meta = Array.from(row.children)
      .slice(1)
      .map((c) => c.className);
    expect(meta[0]).toContain("max-sm:before:content-['Role_']");
    expect(meta[0]).toContain("max-sm:whitespace-normal");
    expect(meta[0]).toContain("max-sm:overflow-visible");
    expect(meta[0]).toContain("max-sm:break-words");
    expect(meta[1]).toContain("max-sm:before:content-['Shares_']");
    expect(meta[2]).toContain("max-sm:before:content-['Ownership_']");
    expect(row.parentElement!.previousElementSibling!.className).toContain(
      "max-sm:hidden",
    );
  });

  it("below sm the Business activities tag sits under the activity text, in a two-column row", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ kbli_code: "70209, 62011" }),
    ]);
    renderTab();
    const tags = await screen.findAllByText("Secondary");
    const cell = tags[0].parentElement!;
    expect(cell.className).toContain("max-sm:col-start-2");
    expect(cell.parentElement!.className).toContain(
      "max-sm:grid-cols-[72px_minmax(0,1fr)]",
    );
  });

  it("the Company identity actions wrap under the title below sm", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([link()]);
    renderTab();
    const sync = await screen.findByRole("button", { name: /Sync Drive/ });
    const actions = sync.closest("div.ml-auto")!;
    expect(actions.className).toContain("max-sm:basis-full");
    expect(actions.className).toContain("max-sm:flex-wrap");
    expect(actions.parentElement!.className).toContain("max-sm:flex-wrap");
  });
});

describe("CompanyTab — Drive source attribution", () => {
  it("does not request Drive sources for a by-name fallback match", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([]);
    mockSearchCompanyByName.mockResolvedValueOnce({
      ...link(),
      id: 90,
      company_name: "Synthetic name match",
    });
    render(
      <CompanyTab
        clientId={412}
        client={{ ...CLIENT, company_name: "Synthetic" }}
        documents={[]}
        formatDate={(d) => d}
        onRefresh={async () => {}}
      />,
    );
    await screen.findByText("Synthetic name match");
    expect(
      screen.queryByRole("region", { name: "Company Drive sources" }),
    ).toBeNull();
    expect(mockGetCompanyDriveSources).not.toHaveBeenCalled();
  });

  it.each([
    { status: "inactive", link_status: undefined },
    { status: undefined, link_status: "inactive" },
    { status: undefined, link_status: undefined },
    { status: "active", link_status: "inactive" },
    { status: "inactive", link_status: "active" },
  ])(
    "does not request Drive sources for an unconfirmed active link %j",
    async (statuses) => {
      if (
        statuses.status === "inactive" &&
        statuses.link_status === undefined
      ) {
        render(
          <CompanyTab
            clientId={412}
            client={CLIENT}
            documents={[]}
            formatDate={(d) => d}
            onRefresh={async () => {}}
            companyLinks={[link(statuses)]}
          />,
        );
      } else {
        mockGetClientCompanies.mockResolvedValueOnce([link(statuses)]);
        renderTab();
      }
      await screen.findByText("PT Synthetic Studio");
      expect(
        screen.queryByRole("region", { name: "Company Drive sources" }),
      ).toBeNull();
      expect(mockGetCompanyDriveSources).not.toHaveBeenCalled();
    },
  );

  it("requests sources for the exact active by-client link_status selection", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ status: undefined, link_status: "active" }),
    ]);
    renderTab();
    await screen.findByRole("link", { name: "Primary" });
    expect(mockGetCompanyDriveSources).toHaveBeenCalledExactlyOnceWith(90);
  });

  it("does not borrow active status from another linked company", async () => {
    mockGetClientCompanies.mockResolvedValueOnce([
      link({ status: "inactive", is_primary: true }),
      link({ company_id: 91, link_id: 2, status: "active", is_primary: false }),
    ]);
    renderTab();
    await screen.findByRole("list", { name: "Other linked companies" });
    expect(
      screen.queryByRole("region", { name: "Company Drive sources" }),
    ).toBeNull();
    expect(mockGetCompanyDriveSources).not.toHaveBeenCalled();
  });

  it("rechecks profile selection and hides stale sources when its link becomes inactive", async () => {
    const props = {
      clientId: 412,
      client: CLIENT,
      documents: [],
      formatDate: (d: string) => d,
      onRefresh: async () => {},
    };
    const { rerender } = render(
      <CompanyTab {...props} companyLinks={[link()]} />,
    );
    const first = await screen.findByRole("link", { name: "Primary" });
    expect(first).toHaveAttribute("href", expect.stringContaining("_90"));

    const second = link({
      company_id: 91,
      link_id: 2,
      company_name: "PT Second Synthetic",
    });
    rerender(<CompanyTab {...props} companyLinks={[second]} />);
    expect(screen.queryByRole("link", { name: "Primary" })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Primary" })).toHaveAttribute(
        "href",
        expect.stringContaining("_91"),
      ),
    );
    expect(mockGetCompanyDriveSources.mock.calls.map(([id]) => id)).toEqual([
      90, 91,
    ]);

    rerender(
      <CompanyTab
        {...props}
        companyLinks={[{ ...second, status: "inactive" }]}
      />,
    );
    expect(
      screen.queryByRole("region", { name: "Company Drive sources" }),
    ).toBeNull();
    await waitFor(() =>
      expect(screen.getByText("PT Second Synthetic")).toBeInTheDocument(),
    );
    expect(mockGetCompanyDriveSources.mock.calls.map(([id]) => id)).toEqual([
      90, 91,
    ]);
  });
});
