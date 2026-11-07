/**
 * One day, and every void document listed once (DOCUMENT_VALIDITY_SPEC.md,
 * obligations 4 and 11), through the real page and the real tabs.
 *
 * The day of the card is the `as_of` of the profile payload on screen. The
 * browser clock only asks for a fresh payload. Synthetic fixtures only.
 */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ClientCompanyLink,
  ClientDocument,
  ClientProfile,
  FamilyMember,
} from "@/lib/api/crm/crm.types";

const {
  mockUseClientDetail,
  mockInvalidateProfile,
  stableTimeline,
  stableSearchParams,
  taxTabProps,
  companyTabProps,
} = vi.hoisted(() => ({
  mockUseClientDetail: vi.fn(),
  mockInvalidateProfile: vi.fn(),
  stableTimeline: [],
  stableSearchParams: { get: vi.fn((_key: string): string | null => null) },
  taxTabProps: [] as Record<string, unknown>[],
  companyTabProps: [] as Record<string, unknown>[],
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "7" }),
  useRouter: () => ({ back: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => stableSearchParams,
}));

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({ email: "staff@example.test" }),
    getUserProfile: vi.fn(() => ({ email: "staff@example.test" })),
    post: vi.fn(),
    request: vi.fn(),
    crm: {
      updateClient: vi.fn(),
      createInteraction: vi.fn(),
      deleteClient: vi.fn(),
      deleteDocument: vi.fn(),
      extractPassportForClient: vi.fn(),
      getClientRecap: vi.fn().mockRejectedValue(new Error("no recap")),
      getClientAiSummary: vi
        .fn()
        .mockResolvedValue({ status: "not_generated" }),
      getClientWaCaseIntelligence: vi
        .fn()
        .mockResolvedValue({ status: "not_generated", cases: [] }),
      getPortalStatus: vi.fn().mockResolvedValue({ has_portal_access: false }),
      getPortalMessages: vi.fn().mockResolvedValue({ messages: [] }),
    },
  },
}));

vi.mock("@/hooks/useClientDetail", () => ({
  useClientDetail: mockUseClientDetail,
  useClientTimeline: () => ({ data: stableTimeline }),
  useDocumentCategories: () => ({ data: [] }),
  useClientBusinessStory: () => ({ data: [], error: null, isLoading: false }),
  useInvalidateClient: () => vi.fn().mockResolvedValue(undefined),
  useInvalidateClientProfile: () => mockInvalidateProfile,
  useSetClientCache: () => vi.fn(),
}));
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [] }),
}));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

vi.mock("./components/AiSummaryCard", () => ({ AiSummaryCard: () => null }));
vi.mock("./components/WaCaseIntelligencePanel", () => ({
  WaCaseIntelligencePanel: () => null,
}));
vi.mock("./components/VaultCard", () => ({ VaultCard: () => null }));
vi.mock("./components/PortalMessages", () => ({ PortalMessages: () => null }));
vi.mock("./components/BusinessStoryPanel", () => ({
  BusinessStoryPanel: () => null,
}));
vi.mock("./components/FilesBrowserPanel", () => ({
  FilesBrowserPanel: () => <div data-testid="FilesBrowserPanel" />,
}));
vi.mock("./components/CompanyTab", () => ({
  CompanyTab: (props: Record<string, unknown>) => {
    companyTabProps.push(props);
    return <div data-testid="CompanyTab" />;
  },
}));
vi.mock("./components/TaxTab", () => ({
  TaxTab: (props: Record<string, unknown>) => {
    taxTabProps.push(props);
    return <div data-testid="TaxTab" />;
  },
}));
vi.mock("./components/modals/EditClientModal", () => ({
  EditClientModal: () => null,
}));
vi.mock("./components/modals/AddFamilyMemberModal", () => ({
  AddFamilyMemberModal: () => null,
}));
vi.mock("./components/modals/EditFamilyMemberModal", () => ({
  EditFamilyMemberModal: () => null,
}));
vi.mock("./components/modals/AddDocumentModal", () => ({
  AddDocumentModal: () => null,
}));
vi.mock("./components/modals/EditDocumentModal", () => ({
  EditDocumentModal: () => null,
}));
vi.mock("./components/modals/AddCompanyModal", () => ({
  AddCompanyModal: () => null,
}));

const last = <T,>(items: T[]): T | undefined => items[items.length - 1];

const CONSULTANTS = [{ value: "consultant@example.test", label: "Consultant" }];

const LINK: ClientCompanyLink = {
  link_id: 701,
  company_id: 7001,
  company_name: "Synthetic PT",
  company_type: "PT PMA",
  role: "shareholder",
  is_primary: true,
  status: "active",
};

const MEMBER = {
  id: 5,
  client_id: 7,
  full_name: "Family Member A",
  relationship: "spouse",
  current_visa_type: "D12",
  visa_expiry: "2026-11-10",
} as FamilyMember;

const doc = (
  over: Partial<ClientDocument> & { id: number; file_name: string },
): ClientDocument => ({
  client_id: 7,
  document_type: "kitas",
  document_category: "immigration",
  ...over,
});

const profileOf = (
  documents: ClientDocument[],
  over: Partial<ClientProfile> = {},
): ClientProfile => ({
  client: {
    id: 7,
    uuid: "synthetic-7",
    full_name: "Synthetic Client",
    email: "synthetic@example.test",
    status: "active",
    client_type: "individual",
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:00:00Z",
    passport_expiry: "2026-11-12",
    passport_number: "X0000007",
    google_drive_folder_id: "synthetic-folder",
  },
  family_members: [],
  documents,
  expiry_alerts: [],
  practices: [],
  company_links: [LINK],
  stats: {
    family_count: 0,
    documents_count: documents.length,
    practices_count: 0,
    expired_count: 0,
    red_alerts: 0,
    yellow_alerts: 0,
  },
  ...over,
});

const mountPage = async (profile: ClientProfile) => {
  mockUseClientDetail.mockReturnValue({
    data: profile,
    isLoading: false,
    error: null,
  });
  const { ClientDetailClient } = await import("./ClientDetailClient");
  const view = render(<ClientDetailClient taxConsultants={CONSULTANTS} />);
  return {
    ...view,
    land: (next: ClientProfile) => {
      mockUseClientDetail.mockReturnValue({
        data: next,
        isLoading: false,
        error: null,
      });
      view.rerender(<ClientDetailClient taxConsultants={CONSULTANTS} />);
    },
  };
};

beforeEach(() => {
  vi.clearAllMocks();
  stableSearchParams.get.mockImplementation(() => null);
  taxTabProps.length = 0;
  companyTabProps.length = 0;
});
afterEach(() => vi.useRealTimers());

const text = () => document.body.textContent ?? "";

// ---------------------------------------------------------------------------
// One day
// ---------------------------------------------------------------------------

describe("one day: the card follows the payload's as_of, never the browser", () => {
  const permit = doc({
    id: 1,
    file_name: "permit.pdf",
    expiry_date: "2026-11-10",
    issue_date: "2026-05-10",
  });
  const passportDoc = doc({
    id: 2,
    file_name: "passport.pdf",
    document_type: "passport",
    document_category: "personal",
    expiry_date: "2026-11-12",
  });
  const general = doc({
    id: 3,
    file_name: "statement.pdf",
    document_type: "bank_statement",
    document_category: "other",
    expiry_date: "2026-11-20",
  });
  const asOf = (day: string) =>
    profileOf([permit, passportDoc, general], {
      as_of: day,
      family_members: [MEMBER],
    });

  it("with the browser day past as_of and the refetch not landed, every surface shows the old day", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-12T04:00:00Z"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await mountPage(asOf("2026-11-07"));

    // Overview: the Visa card (3 days from the card's day, not already past).
    expect(screen.getByTestId("visa-card")).toHaveTextContent("3 days left");
    // Immigration
    await user.click(screen.getByRole("button", { name: "Immigration" }));
    await screen.findByText("Current permit");
    expect(text()).toContain("⏰ 3d left");
    // Documents
    await user.click(screen.getByRole("button", { name: /^Documents/ }));
    await screen.findByLabelText("Documents");
    expect(text()).toContain("⏰ 13d left");
    // Family
    await user.click(screen.getByRole("button", { name: /^Family \(/ }));
    await screen.findByTestId("family-member-row-5");
    expect(text()).toContain("⏰ 3d left");
    expect(text()).not.toMatch(/Expired/);
  });

  it("when the refetch lands with a new as_of, server-derived and local surfaces change in the same render", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-12T04:00:00Z"));
    const page = await mountPage(asOf("2026-11-07"));
    expect(screen.getByTestId("visa-card")).toHaveTextContent("3 days left");

    page.land(asOf("2026-11-09"));
    expect(screen.getByTestId("visa-card")).toHaveTextContent("1 day left");
    expect(screen.getByTestId("visa-card")).not.toHaveTextContent(
      "3 days left",
    );
  });

  it("an as_of in the future of the browser is still the card's day", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-01T04:00:00Z"));
    await mountPage(asOf("2026-11-09"));
    expect(screen.getByTestId("visa-card")).toHaveTextContent("1 day left");
  });

  it("without as_of (an older backend) the card's day is the WITA day at receipt, one value for the whole card", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-07T04:00:00Z"));
    const profile = profileOf([permit]);
    const page = await mountPage(profile);
    expect(screen.getByTestId("visa-card")).toHaveTextContent("3 days left");
    // The browser moves on; the same payload object keeps its day.
    vi.setSystemTime(new Date("2026-11-09T04:00:00Z"));
    page.land(profile);
    expect(screen.getByTestId("visa-card")).toHaveTextContent("3 days left");
  });

  it("forwards the card's day to the Tax tab, whose SPT path uses it", async () => {
    stableSearchParams.get.mockImplementation((k: string) =>
      k === "tab" ? "tax" : null,
    );
    await mountPage(asOf("2026-11-07"));
    await screen.findByTestId("TaxTab");
    expect(last(taxTabProps)?.day).toBe("2026-11-07");
  });
});

describe("the WitaClock asks for fresh data and never sets the day", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  const permitOnly = (day: string) =>
    profileOf(
      [
        doc({
          id: 1,
          file_name: "permit.pdf",
          expiry_date: "2027-01-01",
        }),
      ],
      { as_of: day },
    );

  it("does nothing while the browser's WITA day equals as_of", async () => {
    vi.setSystemTime(new Date("2026-11-07T04:00:00Z"));
    await mountPage(permitOnly("2026-11-07"));
    await act(async () => {
      vi.advanceTimersByTime(5 * 60_000);
    });
    expect(mockInvalidateProfile).not.toHaveBeenCalled();
  });

  it("invalidates the profile on a tick once the browser day is past as_of", async () => {
    vi.setSystemTime(new Date("2026-11-08T04:00:00Z"));
    await mountPage(permitOnly("2026-11-07"));
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(mockInvalidateProfile).toHaveBeenCalledTimes(1);
  });

  it("the midnight timer fires at 00:00 WITA, before the next 60 s tick", async () => {
    // 23:59:40 WITA on 7 Nov = 15:59:40Z. The next tick is 60 s away; the
    // midnight timer is 21 s away (20 s + 1 s of margin).
    vi.setSystemTime(new Date("2026-11-07T15:59:40Z"));
    await mountPage(permitOnly("2026-11-07"));
    await act(async () => {
      vi.advanceTimersByTime(19_000);
    });
    expect(mockInvalidateProfile).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    expect(mockInvalidateProfile).toHaveBeenCalledTimes(1);
  });

  it("a window focus after midnight triggers it too", async () => {
    vi.setSystemTime(new Date("2026-11-08T04:00:00Z"));
    await mountPage(permitOnly("2026-11-07"));
    expect(mockInvalidateProfile).not.toHaveBeenCalled();
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(mockInvalidateProfile).toHaveBeenCalledTimes(1);
  });

  it("a focus on the same day does not", async () => {
    vi.setSystemTime(new Date("2026-11-07T04:00:00Z"));
    await mountPage(permitOnly("2026-11-07"));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(mockInvalidateProfile).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Visibility (obligation 11)
// ---------------------------------------------------------------------------

describe("every void document is listed once, in Removed or rejected", () => {
  const DAY = "2026-11-07";
  const voidFields = { status: "rejected" as const };

  const voidPassport = doc({
    id: 11,
    file_name: "void-passport.pdf",
    document_type: "passport",
    document_category: "personal",
    ...voidFields,
  });
  const voidPermit = doc({
    id: 12,
    file_name: "void-permit.pdf",
    expiry_date: "2027-05-01",
    ...voidFields,
  });
  const voidFamilyPermit = doc({
    id: 13,
    file_name: "void-family-permit.pdf",
    family_member_id: 5,
    expiry_date: "2027-05-01",
    ...voidFields,
  });
  const voidPma = doc({
    id: 14,
    file_name: "void-pma.pdf",
    document_type: "nib",
    document_category: "pma",
    ...voidFields,
  });
  const voidGeneral = doc({
    id: 15,
    file_name: "void-general.pdf",
    document_type: "bank_statement",
    document_category: "other",
    ...voidFields,
  });
  const voidTax = doc({
    id: 16,
    file_name: "void-tax.pdf",
    document_type: "spt_personal",
    document_category: "tax",
    fiscal_year: 2025,
    ...voidFields,
  });
  const VOIDS = [
    voidPassport,
    voidPermit,
    voidFamilyPermit,
    voidPma,
    voidGeneral,
    voidTax,
  ];

  const livePermit = doc({
    id: 21,
    file_name: "live-permit.pdf",
    expiry_date: "2027-03-01",
  });
  const liveGeneral = doc({
    id: 22,
    file_name: "live-general.pdf",
    document_type: "bank_statement",
    document_category: "other",
  });
  const livePassport = doc({
    id: 23,
    file_name: "live-passport.pdf",
    document_type: "passport",
    document_category: "personal",
    created_at: "2020-01-01T00:00:00Z",
  });

  const fullProfile = () =>
    profileOf([...VOIDS, livePermit, liveGeneral, livePassport], {
      as_of: DAY,
      family_members: [{ ...MEMBER, visa_expiry: undefined }],
    });

  const openTab = async (name: RegExp | string) => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name }));
    // The tabs are loaded on demand: wait for the one just opened.
    const marker = String(name).includes("Documents")
      ? () => screen.findByLabelText("Documents")
      : String(name).includes("Immigration")
        ? () => screen.findByText("Immigration", { selector: "h3" })
        : String(name).includes("Family")
          ? () => screen.findByTestId("family-member-row-5")
          : () => screen.findByTestId("CompanyTab");
    await marker();
    return user;
  };

  it("Documents: all six void files appear exactly once, in the group; the badge, strip and pills count the rows", async () => {
    await mountPage(fullProfile());
    // 6 void + 1 live general (the live permit and passport belong elsewhere)
    expect(
      screen.getByRole("button", { name: "Documents (7)" }),
    ).toBeInTheDocument();
    const user = await openTab(/^Documents/);
    expect(screen.getByLabelText("Documents")).toHaveTextContent("7");
    await user.click(
      screen.getByRole("button", { name: "Removed or rejected · 6" }),
    );
    expect(screen.getByLabelText("Documents")).toHaveTextContent("6");
    for (const v of VOIDS) {
      expect(screen.getAllByText(v.file_name!)).toHaveLength(1);
    }
    expect(screen.queryByText("live-general.pdf")).not.toBeInTheDocument();
    expect(screen.getByTestId("FilesBrowserPanel")).toBeInTheDocument();
  });

  it("Documents, All: each void file once even with the live ones listed", async () => {
    await mountPage(fullProfile());
    await openTab(/^Documents/);
    for (const v of VOIDS) {
      expect(screen.getAllByText(v.file_name!)).toHaveLength(1);
    }
    expect(screen.getAllByText("live-general.pdf")).toHaveLength(1);
  });

  it("Overview: the live passport is current, the void one is not, the signpost opens the group", async () => {
    await mountPage(fullProfile());
    const passportCard = screen.getByTestId("passport-card");
    expect(within(passportCard).getByTestId("passport-file")).toHaveTextContent(
      "live-passport.pdf",
    );
    expect(passportCard).not.toHaveTextContent("void-passport.pdf");
    const user = userEvent.setup();
    await user.click(
      within(passportCard).getByRole("button", {
        name: "1 removed or rejected file is in Documents",
      }),
    );
    // The Documents tab is open on the group, with the void files only.
    expect(screen.getByLabelText("Documents")).toHaveTextContent("6");
    expect(
      screen.getByRole("button", { name: "Removed or rejected · 6" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("Immigration: the void own permit and the void family permit are in no list; one signpost for the own permit", async () => {
    await mountPage(fullProfile());
    await openTab("Immigration");
    expect(text()).not.toContain("void-permit.pdf");
    expect(text()).not.toContain("void-family-permit.pdf");
    expect(
      screen.getAllByText("1 removed or rejected file is in Documents"),
    ).toHaveLength(1);
    // the live permit is the current one
    expect(screen.getByText("Current permit")).toBeInTheDocument();
    // the void family permit is neither a holding count nor the family line
    expect(
      screen.queryByText(/Family permits are in the Family tab/),
    ).toBeNull();
  });

  it("Family: the void family permit is not a member's permit; one signpost", async () => {
    await mountPage(fullProfile());
    await openTab(/^Family \(/);
    expect(text()).not.toContain("void-family-permit.pdf");
    expect(screen.getByText("Family Member A")).toBeInTheDocument();
    expect(
      screen.getAllByText("1 removed or rejected file is in Documents"),
    ).toHaveLength(1);
  });

  it("Company and Tax receive the void files, which they themselves refuse (their own tests pin that)", async () => {
    stableSearchParams.get.mockImplementation((k: string) =>
      k === "tab" ? "tax" : null,
    );
    await mountPage(fullProfile());
    const taxDocs = last(taxTabProps)?.documents as ClientDocument[];
    expect(taxDocs.some((d) => d.file_name === "void-tax.pdf")).toBe(true);
    await openTab("Company");
    const companyDocs = last(companyTabProps)?.documents as ClientDocument[];
    expect(companyDocs.some((d) => d.file_name === "void-pma.pdf")).toBe(true);
  });

  it("INNOCENCE: with no void document there is no group and no signpost anywhere", async () => {
    await mountPage(
      profileOf([livePermit, liveGeneral, livePassport], { as_of: DAY }),
    );
    expect(screen.queryByTestId("removed-signpost")).toBeNull();
    await openTab(/^Documents/);
    expect(
      screen.queryByRole("button", { name: /Removed or rejected/ }),
    ).toBeNull();
    expect(screen.getByLabelText("Documents")).toHaveTextContent("1");
  });
});
