import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewTab, overviewLedgerCount } from "./OverviewTab";
import { api } from "@/lib/api";
import type {
  AttentionItem,
  ClientDocument,
  ClientProfile,
} from "@/lib/api/crm/crm.types";

// The redesigned Overview (owner 2026-11-03): needs-attention ledger, three
// distinct cards, the vault, then the rest. Synthetic data only.

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getClientAiSummary: vi.fn(),
      getClientWaCaseIntelligence: vi.fn(),
      deleteClient: vi.fn(),
      extractPassportForClient: vi.fn(),
    },
    post: vi.fn(),
    request: vi.fn(),
  },
}));
vi.mock("./VaultCard", () => ({
  VaultCard: () => <div data-testid="vault-card" />,
}));

const client = {
  id: 42,
  uuid: "uuid-42",
  full_name: "Sample Client",
  email: "sample.client@example.test",
  phone: "081234567890",
  nationality: "Exampleland",
  gender: "F",
  date_of_birth: "1990-05-20",
  birthplace: "Sample City",
  address: "Jl. Contoh No. 1, Sample Town",
  passport_number: "X0000001",
  passport_expiry: "2030-06-01",
  status: "active",
  client_type: "individual",
  created_at: "2026-01-01T00:00:00Z",
} as unknown as ClientProfile["client"];

const stats = {
  family_count: 0,
  documents_count: 0,
  practices_count: 0,
  expired_count: 0,
  red_alerts: 0,
  yellow_alerts: 0,
} as ClientProfile["stats"];

const passportDoc = {
  id: 1,
  client_id: 42,
  document_type: "passport",
  document_category: "personal",
  file_name: "scan.jpg",
  google_drive_file_url: "https://drive.google.com/file/d/drive-pass-1/view",
} as ClientDocument;

const visaDoc = {
  id: 2,
  client_id: 42,
  document_type: "kitas",
  document_category: "immigration",
  file_name: "kitas.pdf",
  google_drive_file_url: "https://drive.google.com/file/d/drive-visa-1/view",
  issue_date: "2026-07-01",
  expiry_date: "2027-01-02",
  permit_code: "E28A",
  permit_label: "E28A — Investor KITAS",
  permit_family_label: "KITAS / ITAS — Limited Stay Permit",
  permit_sponsor: "PT Contoh Abadi",
} as ClientDocument;

const baseProps = {
  client,
  stats,
  documents: [passportDoc, visaDoc],
  activePractices: [],
  completedPractices: [],
  needsViewerAction: false,
  formatDate: (d: string) => d,
  formatCurrency: (n: number) => String(n),
  onEditClick: vi.fn(),
  onDeleteClick: vi.fn(),
  canDeleteClient: true,
  isDeletingClient: false,
  onRefresh: vi.fn().mockResolvedValue(undefined),
  clientId: 42,
};

const item = (over: Partial<AttentionItem>): AttentionItem => ({
  kind: "permit",
  subject: "client",
  subject_id: 42,
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

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.crm.getClientAiSummary).mockResolvedValue({
    status: "not_generated",
  } as never);
  vi.mocked(api.crm.getClientWaCaseIntelligence).mockResolvedValue({
    status: "not_generated",
    cases: [],
  } as never);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-03T04:00:00Z"));
});
afterEach(() => vi.useRealTimers());

describe("Needs attention — one row per area, the rest behind Show N more", () => {
  const section = () =>
    screen
      .getByRole("heading", { name: "Needs attention" })
      .closest("section")!;
  const headline = () =>
    within(screen.getByTestId("attention-rows")).getAllByRole("listitem");

  it("INNOCENCE: Visa, Company, Tax — three rows, each with its state word", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ state: "urgent", days: 62, date: "2027-01-04" }),
          item({
            kind: "lkpm",
            label: "LKPM Q4 2026",
            subject: "company",
            subject_name: "PT Contoh Abadi",
            state: "upcoming",
            days: 6,
            opens_on: "2026-11-09",
          }),
          item({
            kind: "spt_personal",
            label: "SPT personal 2025",
            state: "done",
          }),
        ]}
      />,
    );
    const li = headline();
    expect(li).toHaveLength(3);
    expect(li[0].textContent).toContain("Visa");
    expect(li[0].textContent).toContain("KITAS expires 4 Jan 2027 · 62 days");
    expect(li[0].textContent).toContain("Urgent");
    expect(li[1].textContent).toContain(
      "LKPM Q4 2026 opens 9 Nov 2026 · in 6 days · PT Contoh Abadi",
    );
    expect(li[1].textContent).toContain("Upcoming");
    expect(li[2].textContent).toContain("SPT personal 2025 filed");
    expect(li[2].textContent).toContain("Done");
    expect(screen.queryByTestId("attention-more")).toBeNull();
    expect(within(section()).queryByText("Wait")).toBeNull();
    expect(within(section()).queryByText("You")).toBeNull();
  });

  it("the most pressing item wins each area; the rest count in Show N more", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ label: "KITAS", state: "urgent", days: 20 }),
          item({
            kind: "passport",
            label: "Passport",
            subject: "family_member",
            subject_name: "Sample Child",
            days: 9,
          }),
          item({
            kind: "lkpm",
            label: "LKPM Q3 2026",
            state: "overdue",
            date: "2026-10-15",
          }),
          item({
            kind: "lkpm",
            label: "LKPM Q4 2026",
            state: "upcoming",
            days: 6,
          }),
          item({
            kind: "spt_company",
            label: "SPT company 2025",
            state: "open",
            date: "2027-03-31",
          }),
          item({
            kind: "spt_personal",
            label: "SPT personal 2025",
            state: "done",
          }),
        ]}
      />,
    );
    const li = headline();
    expect(li).toHaveLength(3);
    expect(li[1].textContent).toContain("LKPM Q3 2026 overdue since 15 Oct");
    expect(li[2].textContent).toContain("SPT company 2025 due 31 Mar 2027");
    const more = screen.getByTestId("attention-more");
    expect(within(more).getByText("Show 3 more")).toBeInTheDocument();
    expect(more.tagName).toBe("DETAILS");
    expect(more.textContent).toContain(
      "Sample Child · Passport expires soon · 9 days",
    );
    expect(more.textContent).toContain("LKPM Q4 2026 opens soon · in 6 days");
    expect(more.textContent).toContain("SPT personal 2025 filed");
    // each extra row is labelled by area — never "Other"
    expect(more.textContent).not.toContain("Other");
    const labels = within(more)
      .getAllByRole("listitem")
      .map((li) => li.querySelector("span")!.textContent);
    expect(labels.sort()).toEqual(["Company", "Family", "Tax"]);
  });

  it("severity fills the slots: the urgent family and passport rows show, the filed company and tax rows wait behind Show N more under their own labels", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ days: 62, date: "2027-01-04" }),
          item({
            kind: "passport",
            label: "Passport",
            days: 70,
            date: "2027-01-12",
          }),
          item({
            label: "Spouse KITAS",
            code: "E31A",
            subject: "family_member",
            subject_name: "Sample Spouse",
            days: 20,
            date: "2026-11-23",
          }),
          item({ kind: "lkpm", label: "LKPM Q4 2026", state: "done" }),
          item({ kind: "lkpm", label: "LKPM Q3 2026", state: "done" }),
          item({
            kind: "spt_personal",
            label: "SPT personal 2025",
            state: "done",
          }),
          item({
            kind: "spt_company",
            label: "SPT company 2025",
            state: "done",
          }),
        ]}
      />,
    );
    expect(
      headline().map((li) => li.querySelector("span")!.textContent),
    ).toEqual(["Visa", "Family", "Passport"]);
    expect(headline()[1].textContent).toContain(
      "Sample Spouse · E31A · Spouse KITAS expires 23 Nov 2026 · 20 days",
    );
    const more = screen.getByTestId("attention-more");
    const labels = within(more)
      .getAllByRole("listitem")
      .map((li) => li.querySelector("span")!.textContent);
    expect(labels.sort()).toEqual(["Company", "Company", "Tax", "Tax"]);
  });

  it("GUILT: no lkpm item means no Company row; no spt item means no Tax row", () => {
    render(<OverviewTab {...baseProps} attention={[item({ days: 62 })]} />);
    expect(screen.queryByTestId("ledger-company")).toBeNull();
    expect(screen.queryByTestId("ledger-tax")).toBeNull();
    expect(headline()).toHaveLength(1);
  });

  it("the Visa row is the client's own permit, not a family member's", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[]}
        attention={[
          item({
            subject: "family_member",
            subject_name: "Sample Child",
            days: 5,
          }),
        ]}
      />,
    );
    expect(screen.queryByTestId("ledger-visa")).toBeNull();
    // with nothing else to show, the family item fills the free slot
    expect(screen.getByTestId("ledger-family").textContent).toContain(
      "Sample Child",
    );
  });

  it("permit + urgent passport + urgent spouse: three visible, none hidden", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ days: 40, date: "2026-12-13" }),
          item({
            kind: "passport",
            label: "Passport",
            state: "urgent",
            days: 20,
            date: "2026-11-23",
          }),
          item({
            label: "Spouse KITAS",
            code: "E31A",
            subject: "family_member",
            subject_name: "Sample Spouse",
            state: "urgent",
            days: 10,
            date: "2026-11-13",
          }),
        ]}
      />,
    );
    const li = headline();
    expect(li).toHaveLength(3);
    // Visa first, then the free slots by pressing order (spouse 10d, passport 20d)
    expect(li.map((x) => x.querySelector("span")!.textContent)).toEqual([
      "Visa",
      "Family",
      "Passport",
    ]);
    expect(screen.queryByTestId("attention-more")).toBeNull();
  });

  it("Visa + Company + Tax + three others: three visible, Show 3 more, closed", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ days: 40, date: "2026-12-13" }),
          item({
            kind: "lkpm",
            label: "LKPM Q4 2026",
            state: "open",
            date: "2027-01-15",
          }),
          item({
            kind: "spt_personal",
            label: "SPT personal 2025",
            state: "open",
            date: "2027-03-31",
          }),
          item({
            kind: "passport",
            label: "Passport",
            state: "urgent",
            days: 20,
          }),
          item({
            label: "Spouse KITAS",
            subject: "family_member",
            subject_name: "Sample Spouse",
            state: "urgent",
            days: 10,
          }),
          item({ kind: "lkpm", label: "LKPM Q3 2026", state: "done" }),
        ]}
      />,
    );
    expect(headline()).toHaveLength(3);
    const more = screen.getByTestId("attention-more");
    expect(within(more).getByText("Show 3 more")).toBeInTheDocument();
    expect((more as HTMLDetailsElement).open).toBe(false);
  });

  it("GUILT: a done item never fills a free slot; it waits behind Show more", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({ days: 40, date: "2026-12-13" }),
          item({ kind: "passport", label: "Passport", state: "done" }),
        ]}
      />,
    );
    expect(headline()).toHaveLength(1);
    const more = screen.getByTestId("attention-more");
    expect(within(more).getByText("Show 1 more")).toBeInTheDocument();
    expect(more.textContent).toContain("Passport valid");
  });

  it("an expired permit item reads in the strong tone with its word", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[item({ state: "expired", days: -3 })]}
      />,
    );
    const tag = within(screen.getByTestId("ledger-visa")).getByTestId(
      "attention-state",
    );
    expect(tag.textContent).toBe("Expired");
    expect(tag.className).toContain("font-bold");
  });

  it("a visit visa shows its ITK-based date and ITK missing", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[
          item({
            label: "Visit visa",
            state: "urgent",
            days: 11,
            date: "2026-11-14",
            note: "itk_missing",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("ledger-visa").textContent).toContain(
      "Visit visa expires 14 Nov 2026 · 11 days · ITK missing",
    );
  });
});

describe("Needs attention — without `attention` (backend not deployed yet)", () => {
  it("INNOCENCE: only the Visa row, built from the current permit, calm", () => {
    render(<OverviewTab {...baseProps} />);
    const li = within(screen.getByTestId("attention-rows")).getAllByRole(
      "listitem",
    );
    expect(li).toHaveLength(1);
    expect(li[0].textContent).toContain(
      "E28A · Investor KITAS — valid until 2 Jan 2027 · 60 days",
    );
    const tag = within(li[0]).getByTestId("attention-state");
    expect(tag.textContent).toBe("Expiring");
    expect(screen.queryByTestId("attention-more")).toBeNull();
  });

  it("a far-off permit is Valid, calm, with the remaining days", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[{ ...visaDoc, expiry_date: "2027-06-25" }]}
      />,
    );
    const row = screen.getByTestId("ledger-visa");
    expect(row.textContent).toContain("valid until 25 Jun 2027 · 234 days");
    const tag = within(row).getByTestId("attention-state");
    expect(tag.textContent).toBe("Valid");
    expect(tag.className).toContain("text-[var(--tx-secondary)]");
  });

  it("an expired permit says so with the strong tone", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[{ ...visaDoc, expiry_date: "2026-10-31" }]}
      />,
    );
    expect(screen.getByTestId("ledger-visa").textContent).toContain(
      "expired 3 days ago",
    );
  });

  it("GUILT: no permit, no attention: no ledger at all — and never the old alert list", () => {
    render(<OverviewTab {...baseProps} documents={[]} />);
    expect(
      screen.queryByRole("heading", { name: "Needs attention" }),
    ).toBeNull();
    expect(screen.queryByText(/expires soon/)).toBeNull();
    expect(screen.queryByText("Show all", { exact: false })).toBeNull();
  });

  it("the permit label carries no qualifier", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[
          { ...visaDoc, permit_label: "E28A — Investor KITAS (Standard)" },
        ]}
      />,
    );
    expect(screen.getByTestId("ledger-visa").textContent).not.toContain(
      "Standard",
    );
    expect(screen.getByTestId("visa-card").textContent).not.toContain(
      "Standard",
    );
  });
});

describe("card titles are the dominant text", () => {
  it.each(["client-info-card", "passport-card", "visa-card"])(
    "%s title uses the 22px/28px display size, the eyebrow stays small",
    (id) => {
      render(<OverviewTab {...baseProps} />);
      const title = screen.getByTestId(`${id}-title`);
      expect(title.className).toContain("text-[22px]");
      expect(title.className).toContain("md:text-[28px]");
      // one line at tablet and desktop; the art column shrinks instead
      expect(title.className).toContain("md:whitespace-nowrap");
      const art = within(screen.getByTestId(`${id}-header`)).getByTestId(
        /^card-art-/,
      );
      expect(art.className).toContain("max-w-[38%]");
      expect(art.className).toContain("min-w-0");
      expect(art.className).not.toContain("shrink-0");
      const eyebrow = within(screen.getByTestId(`${id}-header`)).getByText(
        /^(Contact|Identity document|Stay permit)$/,
      );
      expect(eyebrow.className).toContain("text-[12px]");
    },
  );
});

describe("the three cards", () => {
  it("Client info carries name, email, phone, nationality, gender, age, address", () => {
    render(<OverviewTab {...baseProps} />);
    const card = within(screen.getByTestId("client-info-card"));
    expect(card.getByText("Sample Client")).toBeInTheDocument();
    expect(
      card.getByRole("link", { name: "sample.client@example.test" }),
    ).toHaveAttribute("href", "mailto:sample.client@example.test");
    expect(card.getByRole("link", { name: /Call/ })).toHaveAttribute(
      "href",
      "tel:081234567890",
    );
    expect(card.getByRole("link", { name: /WhatsApp/ })).toHaveAttribute(
      "href",
      "https://wa.me/6281234567890",
    );
    expect(card.getByText("Exampleland")).toBeInTheDocument();
    expect(card.getByText("Female")).toBeInTheDocument();
    expect(card.getByText("36 y")).toBeInTheDocument();
    expect(card.getByText(/born 1990-05-20/)).toBeInTheDocument();
    expect(card.getByText("Jl. Contoh No. 1, Sample Town")).toBeInTheDocument();
    expect(card.getByLabelText("Edit client info")).toBeInTheDocument();
    expect(card.getByLabelText("Delete client")).toBeInTheDocument();
  });

  it("Client info: delete is absent when the viewer may not delete", () => {
    render(<OverviewTab {...baseProps} canDeleteClient={false} />);
    expect(screen.queryByLabelText("Delete client")).toBeNull();
  });

  it("Passport carries number, nationality, birth data and the remaining time", () => {
    render(<OverviewTab {...baseProps} />);
    const card = within(screen.getByTestId("passport-card"));
    expect(card.getByText("X0000001")).toBeInTheDocument();
    expect(card.getByText("Exampleland")).toBeInTheDocument();
    expect(card.getByText("1990-05-20")).toBeInTheDocument();
    expect(card.getByText("Sample City")).toBeInTheDocument();
    expect(card.getByText("2030-06-01")).toBeInTheDocument();
    expect(card.getByText(/left$/)).toBeInTheDocument();
    expect(
      card.getByRole("button", { name: "Download passport" }),
    ).toBeEnabled();
    expect(card.getByLabelText("Upload passport")).toBeInTheDocument();
    expect(card.getByRole("button", { name: /Extract/ })).toBeInTheDocument();
    expect(card.getByLabelText("Delete passport")).toBeInTheDocument();
  });

  it("Visa carries code and popular name, kind, dates, remaining, sponsor and address", () => {
    render(<OverviewTab {...baseProps} />);
    const card = within(screen.getByTestId("visa-card"));
    // the permit is said once, under the title — never again as a field
    expect(card.getAllByText("E28A · Investor KITAS")).toHaveLength(1);
    expect(card.queryByText("Permit")).toBeNull();
    // no "Kind" field (it repeated the permit line / showed raw "EVISA")
    expect(card.queryByText("Kind")).toBeNull();
    expect(card.queryByText(/Limited Stay Permit/)).toBeNull();
    // the permit line sits full width BELOW the header zone, not beside the art
    const line = card.getByTestId("visa-permit-name");
    const header = card.getByTestId("visa-card-header");
    expect(header.contains(line)).toBe(false);
    expect(line.className).toContain("text-[17px]");
    expect(
      header.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(card.getByText("2026-07-01")).toBeInTheDocument();
    expect(card.getByText("2027-01-02")).toBeInTheDocument();
    expect(card.getByText("60 days left")).toBeInTheDocument();
    expect(card.getByText("PT Contoh Abadi")).toBeInTheDocument();
    expect(card.getByTestId("permit-runway")).toBeInTheDocument();
    expect(card.getByText("Jl. Contoh No. 1, Sample Town")).toBeInTheDocument();
    expect(card.queryByText(/No address on file/)).toBeNull();
    expect(card.getByRole("button", { name: "Download visa" })).toBeEnabled();
    expect(card.getByLabelText("Upload visa document")).toBeInTheDocument();
  });

  it("GUILT: the Visa card warns in words when the client has no address", () => {
    render(
      <OverviewTab
        {...baseProps}
        client={{ ...client, address: undefined } as never}
      />,
    );
    const box = screen.getByTestId("visa-address");
    expect(box.textContent).toContain(
      "No address on file — needed for the permit",
    );
    expect(box.className).toContain("border-[var(--state-warning)]");
  });

  it("an expired visa says so in words, not only in colour", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[{ ...visaDoc, expiry_date: "2026-10-31" }]}
      />,
    );
    expect(
      within(screen.getByTestId("visa-card")).getByText("Expired 3 days ago"),
    ).toBeInTheDocument();
  });

  it("Download goes through the proxy flow (the existing one)", () => {
    const click = vi.fn();
    const real = document.createElement.bind(document);
    const spy = vi
      .spyOn(document, "createElement")
      .mockImplementation((tag: string, o?: ElementCreationOptions) => {
        const el = real(tag, o);
        if (tag === "a") el.click = click;
        return el;
      });
    try {
      render(<OverviewTab {...baseProps} />);
      fireEvent.click(
        screen.getByRole("button", { name: "Download passport" }),
      );
      expect(click).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: "Download visa" }));
      expect(click).toHaveBeenCalledTimes(2);
    } finally {
      spy.mockRestore();
    }
  });
});

describe("no document thumbnail anywhere on the Overview", () => {
  it("GUILT: with a passport and a visa on file, no <img> points at a document route", () => {
    const { container } = render(<OverviewTab {...baseProps} />);
    const srcs = Array.from(container.querySelectorAll("img")).map(
      (i) => i.getAttribute("src") ?? "",
    );
    expect(srcs.length).toBeGreaterThan(0); // the decorative art is there
    for (const src of srcs) {
      expect(src).not.toContain("/api/documents/");
      expect(src).not.toContain("drive.google.com");
      expect(src).toMatch(/^\/images\/kita\/client-card\/[a-z-]+\.webp$/);
    }
  });

  it("the card art is decorative, lazy and sized; a broken file removes itself", () => {
    const { container } = render(<OverviewTab {...baseProps} />);
    const imgs = Array.from(container.querySelectorAll("img"));
    expect(imgs.map((i) => i.getAttribute("src"))).toEqual([
      "/images/kita/client-card/client-info.webp",
      "/images/kita/client-card/passport.webp",
      "/images/kita/client-card/visa.webp",
    ]);
    for (const img of imgs) {
      expect(img.getAttribute("alt")).toBe("");
      expect(img.getAttribute("aria-hidden")).toBe("true");
      expect(img.getAttribute("loading")).toBe("lazy");
      expect(img.getAttribute("decoding")).toBe("async");
      expect(img.getAttribute("width")).toBeTruthy();
      expect(img.getAttribute("height")).toBeTruthy();
    }
    fireEvent.error(imgs[1]);
    expect(screen.queryByTestId("card-art-passport")).toBeNull();
    expect(screen.getByTestId("card-art-visa")).toBeInTheDocument();
  });
});

describe("illustrations never sit on content", () => {
  it.each([
    ["client-info-card", "client-info"],
    ["passport-card", "passport"],
    ["visa-card", "visa"],
  ])("%s: the art is inside its header zone, not in the fields", (id, art) => {
    render(<OverviewTab {...baseProps} />);
    const header = screen.getByTestId(`${id}-header`);
    const img = screen.getByTestId(`card-art-${art}`);
    expect(header.contains(img)).toBe(true);
    const grid = screen.getByTestId(id).querySelector("dl")!;
    expect(grid.contains(img)).toBe(false);
    expect(
      header.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(img.className).not.toContain("absolute");
  });
});

describe("page order", () => {
  it("attention, then the three cards, then the vault, then open processes, then family", () => {
    render(
      <OverviewTab
        {...baseProps}
        attention={[item({ days: 5 })]}
        activePractices={[
          {
            id: 1,
            status: "on_process",
            practice_type_code: "SYNTHETIC",
            practice_type_name: "Synthetic Process",
          } as ClientProfile["practices"][number],
        ]}
      />,
    );
    const order = [
      screen.getByRole("heading", { name: "Needs attention" }),
      screen.getByTestId("identity-cards"),
      screen.getByTestId("vault-card"),
      screen.getByRole("heading", { name: "Open processes" }),
      screen.getByRole("heading", { name: "Family & companies" }),
    ];
    for (let i = 0; i < order.length - 1; i++) {
      expect(
        order[i].compareDocumentPosition(order[i + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
  });
});

describe("Needs attention — the backend could not build the list", () => {
  it("GUILT: attention_error set with no rows does not read as all clear", () => {
    render(
      <OverviewTab
        {...baseProps}
        documents={[]}
        attention={[]}
        attentionError="attention_build_failed"
      />,
    );
    expect(
      screen.getByRole("heading", { name: "Needs attention" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("attention-unavailable").textContent).toContain(
      "Attention list unavailable — check the Immigration, Company and Tax tabs",
    );
  });

  it("INNOCENCE: attention_error null with no rows shows nothing, and with rows shows no notice", () => {
    const { unmount } = render(
      <OverviewTab
        {...baseProps}
        documents={[]}
        attention={[]}
        attentionError={null}
      />,
    );
    expect(screen.queryByTestId("attention-unavailable")).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Needs attention" }),
    ).toBeNull();
    unmount();
    render(
      <OverviewTab
        {...baseProps}
        attention={[item({ days: 30, date: "2026-12-03" })]}
        attentionError={null}
      />,
    );
    expect(screen.queryByTestId("attention-unavailable")).toBeNull();
    expect(screen.getByTestId("attention-rows")).toBeInTheDocument();
  });

  it("the numbering counts the notice as a section, and Family & companies is always one", () => {
    expect(overviewLedgerCount([], [], [], "attention_build_failed")).toBe(2);
    expect(overviewLedgerCount([], [], [], null)).toBe(1);
  });
});

describe("Needs attention — the per-row Review action", () => {
  const review = (name: RegExp) => screen.getByRole("button", { name });

  const renderWith = (attention: AttentionItem[], onOpenTab = vi.fn()) => {
    render(
      <OverviewTab
        {...baseProps}
        attention={attention}
        onOpenTab={onOpenTab}
      />,
    );
    return onOpenTab;
  };

  it("every row has a 32px Review button whose label names the item", () => {
    renderWith([item({ label: "KITAS", days: 30, date: "2026-12-03" })]);
    const b = review(/^Review KITAS expires/);
    expect(b.className).toContain("min-h-8");
    expect(b.getAttribute("aria-label")).toContain("KITAS");
  });

  it("the client's own permit scrolls to the Visa card and focuses it, opening no tab", () => {
    const onOpenTab = renderWith([item({ days: 30, date: "2026-12-03" })]);
    fireEvent.click(review(/^Review KITAS/));
    const card = screen.getByTestId("visa-card");
    expect(card).toHaveAttribute("id", "visa-card");
    expect(card).toHaveAttribute("tabindex", "-1");
    expect(card.className).toContain("scroll-mt-28");
    expect(document.activeElement).toBe(card);
    expect(onOpenTab).not.toHaveBeenCalled();
  });

  it("the client's own passport goes to the Passport card", () => {
    const onOpenTab = renderWith([
      item({
        kind: "passport",
        label: "Passport",
        days: 30,
        date: "2026-12-03",
      }),
    ]);
    fireEvent.click(review(/^Review Passport/));
    expect(document.activeElement).toBe(screen.getByTestId("passport-card"));
    expect(onOpenTab).not.toHaveBeenCalled();
  });

  it("GUILT: a family member's permit or passport opens the Family tab, never the client's own card", () => {
    const onOpenTab = renderWith([
      item({
        label: "Spouse KITAS",
        subject: "family_member",
        subject_name: "Sample Spouse",
        days: 10,
        date: "2026-11-13",
      }),
      item({
        kind: "passport",
        label: "Passport",
        subject: "family_member",
        subject_name: "Sample Child",
        days: 12,
        date: "2026-11-15",
      }),
    ]);
    fireEvent.click(review(/Sample Spouse/));
    fireEvent.click(review(/Sample Child/));
    expect(onOpenTab.mock.calls).toEqual([["family"], ["family"]]);
    expect(document.activeElement).not.toBe(screen.getByTestId("visa-card"));
    expect(document.activeElement).not.toBe(
      screen.getByTestId("passport-card"),
    );
  });

  it("lkpm opens Company, spt_personal and spt_company open Tax, anything else opens Documents", () => {
    const onOpenTab = renderWith([
      item({
        kind: "lkpm",
        subject: "company",
        label: "LKPM Q4",
        state: "urgent",
        days: 3,
        date: "2026-11-06",
      }),
      item({
        kind: "spt_personal",
        label: "SPT personal",
        state: "urgent",
        days: 4,
        date: "2026-11-07",
      }),
      item({
        kind: "spt_company",
        subject: "company",
        label: "SPT company",
        state: "urgent",
        days: 5,
        date: "2026-11-08",
      }),
      item({
        kind: "permit",
        subject: "company",
        label: "Sponsor document",
        state: "urgent",
        days: 6,
        date: "2026-11-09",
      }),
    ]);
    fireEvent.click(review(/LKPM Q4/));
    fireEvent.click(review(/SPT personal/));
    fireEvent.click(review(/SPT company/));
    fireEvent.click(review(/Sponsor document/));
    expect(onOpenTab.mock.calls.map((c) => c[0])).toEqual([
      "company",
      "tax",
      "tax",
      "documents",
    ]);
  });
});

describe("Client info card — copy buttons are 32px targets", () => {
  it("email and phone copy buttons are at least 32px", () => {
    render(<OverviewTab {...baseProps} attention={[]} />);
    for (const name of ["Copy email", "Copy phone"]) {
      const b = screen.getByRole("button", { name });
      expect(b.className).toContain("min-h-8");
      expect(b.className).toContain("min-w-8");
    }
  });
});

describe("Overview section numbers follow what renders", () => {
  const heads = (container: HTMLElement) =>
    Array.from(container.querySelectorAll("section"))
      .map((sec) => sec.firstElementChild as HTMLElement)
      .filter((head) =>
        /^\d\d$/.test(head.querySelector("span")?.textContent ?? ""),
      )
      .map(
        (head) =>
          `${head.querySelector("span")?.textContent} ${head.querySelector("h2")?.textContent}`,
      );

  it("GUILT: Family & companies is numbered like its siblings, in order after Needs attention and Open processes", () => {
    const { container } = render(
      <OverviewTab
        {...baseProps}
        attention={[item({ days: 30, date: "2026-12-03" })]}
        activePractices={[
          {
            id: 1,
            status: "in_progress",
            practice_type_code: "KITAS_RENEWAL",
            practice_type_name: "KITAS Renewal",
          } as ClientProfile["practices"][number],
        ]}
      />,
    );
    expect(heads(container)).toEqual([
      "01 Needs attention",
      "02 Open processes",
      "03 Family & companies",
    ]);
  });

  it("INNOCENCE: with no ledger and no practices Family & companies is 01, no gap", () => {
    const { container } = render(
      <OverviewTab {...baseProps} documents={[]} attention={[]} />,
    );
    expect(heads(container)).toEqual(["01 Family & companies"]);
  });
});
