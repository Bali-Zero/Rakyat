import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientProfile } from "@/lib/api/crm/crm.types";
import {
  earlyKey,
  resetEarly,
  startEarly,
  takeEarly,
} from "./components/early-fetch";

// Page weight (owner: "make it fast"): only Overview is in the page chunk;
// every other tab and every modal is a lazy chunk, preloaded on hover/focus of
// its tab button; the id-only fetches start at mount, in parallel with the
// profile, instead of after it.

const { tabModuleEvaluated, mockUseClientDetail, apiCrm } = vi.hoisted(() => ({
  // One entry per tab module whose (mocked) factory has actually run, i.e. per
  // chunk the page really fetched.
  tabModuleEvaluated: [] as string[],
  mockUseClientDetail: vi.fn(),
  apiCrm: {
    getClientAiSummary: vi.fn(),
    getClientWaCaseIntelligence: vi.fn(),
    getPortalStatus: vi.fn(),
    getPortalMessages: vi.fn(),
  },
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "7" }),
  useRouter: () => ({ back: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));
vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({ email: "staff@example.test" }),
    getUserProfile: vi.fn(() => ({ email: "staff@example.test" })),
    crm: apiCrm,
  },
}));
vi.mock("@/hooks/useClientDetail", () => ({
  useClientDetail: mockUseClientDetail,
  useClientTimeline: () => ({ data: [] }),
  useDocumentCategories: () => ({ data: [] }),
  useClientBusinessStory: () => ({ data: [], error: null, isLoading: false }),
  useInvalidateClient: () => vi.fn(),
  useSetClientCache: () => vi.fn(),
}));
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [] }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("./components/OverviewTab", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./components/OverviewTab")>()),
  OverviewTab: () => <div data-testid="OverviewTab" />,
}));
vi.mock("./components/BusinessStoryPanel", () => ({
  BusinessStoryPanel: () => null,
}));
vi.mock("./components/TaxTab", () => {
  tabModuleEvaluated.push("tax");
  return { TaxTab: () => <div data-testid="TaxTab" /> };
});
vi.mock("./components/FamilyTab", () => {
  tabModuleEvaluated.push("family");
  return { FamilyTab: () => <div data-testid="FamilyTab" /> };
});

const SRC = readFileSync(join(__dirname, "ClientDetailClient.tsx"), "utf8");

const profile = {
  client: {
    id: 7,
    uuid: "u7",
    full_name: "Synthetic Client",
    status: "active",
    client_type: "individual",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  family_members: [],
  documents: [],
  expiry_alerts: [],
  practices: [],
  company_links: [
    {
      link_id: 1,
      company_id: 1,
      company_name: "PT Contoh",
      company_type: "PT PMA",
      role: "director",
      is_primary: true,
      status: "active",
    },
  ],
  stats: {
    family_count: 0,
    documents_count: 0,
    practices_count: 0,
    expired_count: 0,
    red_alerts: 0,
    yellow_alerts: 0,
  },
} as unknown as ClientProfile;

beforeEach(() => {
  vi.clearAllMocks();
  resetEarly();
  tabModuleEvaluated.length = 0;
  apiCrm.getClientAiSummary.mockResolvedValue({ status: "not_generated" });
  apiCrm.getClientWaCaseIntelligence.mockResolvedValue({
    status: "not_generated",
    cases: [],
  });
  apiCrm.getPortalStatus.mockResolvedValue({ has_portal_access: false });
  apiCrm.getPortalMessages.mockResolvedValue({ messages: [] });
});
afterEach(() => vi.useRealTimers());

describe("the tab and modal modules are not in the static import graph", () => {
  const STATIC_IMPORT = (name: string) =>
    new RegExp(`^import[^;]*from\\s+"\\./components/${name}";`, "m");

  it.each([
    "DocumentsTab",
    "FilesBrowserPanel",
    "ProcessTab",
    "FamilyTab",
    "ImmigrationTab",
    "CompanyTab",
    "TaxTab",
    "modals/EditClientModal",
    "modals/AddFamilyMemberModal",
    "modals/EditFamilyMemberModal",
    "modals/AddDocumentModal",
    "modals/EditDocumentModal",
    "modals/AddCompanyModal",
  ])("%s is loaded by a dynamic import only", (name) => {
    expect(SRC).not.toMatch(STATIC_IMPORT(name));
    expect(SRC).toContain(`import("./components/${name}")`);
  });

  it("INNOCENCE: Overview stays static, and every lazy tab has a skeleton", () => {
    expect(SRC).toMatch(
      /^import \{ OverviewTab[^;]*from "\.\/components\/OverviewTab";/m,
    );
    expect(SRC.match(/loading: \(\) => <TabSkeleton \/>/g)?.length).toBe(6);
  });

  it("a rendered page fetches NO other tab's chunk until it is needed", async () => {
    mockUseClientDetail.mockReturnValue({
      data: profile,
      isLoading: false,
      error: null,
    });
    const { ClientDetailClient } = await import("./ClientDetailClient");
    render(<ClientDetailClient taxConsultants={[]} />);
    expect(await screen.findByTestId("OverviewTab")).toBeInTheDocument();
    expect(tabModuleEvaluated).toEqual([]);
  });

  it("hovering a tab button fetches that tab's chunk (and only that one)", async () => {
    mockUseClientDetail.mockReturnValue({
      data: profile,
      isLoading: false,
      error: null,
    });
    const { ClientDetailClient } = await import("./ClientDetailClient");
    render(<ClientDetailClient taxConsultants={[]} />);
    fireEvent.pointerEnter(screen.getByRole("button", { name: "Tax" }));
    await waitFor(() => expect(tabModuleEvaluated).toEqual(["tax"]));
    fireEvent.focus(screen.getByRole("button", { name: /^Family/ }));
    await waitFor(() => expect(tabModuleEvaluated).toEqual(["tax", "family"]));
  });

  it("exports a loader for every non-overview tab", async () => {
    const { TAB_LOADERS } = await import("./ClientDetailClient");
    expect(Object.keys(TAB_LOADERS).sort()).toEqual(
      ["company", "documents", "family", "process", "tax", "visas"].sort(),
    );
    expect(TAB_LOADERS).not.toHaveProperty("overview");
  });
});

describe("id-only fetches start at mount, in parallel with the profile", () => {
  it("fires before the profile resolves", async () => {
    mockUseClientDetail.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    const { ClientDetailClient } = await import("./ClientDetailClient");
    render(<ClientDetailClient taxConsultants={[]} />);
    await waitFor(() => {
      expect(apiCrm.getClientAiSummary).toHaveBeenCalledWith(7);
      expect(apiCrm.getClientWaCaseIntelligence).toHaveBeenCalledWith(7);
      expect(apiCrm.getPortalStatus).toHaveBeenCalledWith(7);
      expect(apiCrm.getPortalMessages).toHaveBeenCalledWith(7);
    });
    // the profile is still loading: no page, no Overview yet
    expect(screen.queryByTestId("OverviewTab")).toBeNull();
  });
});

describe("early-fetch", () => {
  it("hands a started call to its owner exactly once", async () => {
    const load = vi.fn().mockResolvedValue("data");
    const key = earlyKey("unit", 1);
    startEarly(key, load);
    startEarly(key, load); // a StrictMode double effect starts nothing twice
    expect(load).toHaveBeenCalledTimes(1);
    await expect(takeEarly<string>(key)).resolves.toBe("data");
    expect(takeEarly(key)).toBeNull(); // the next load goes to the network
  });

  it("GUILT: a stale early call is not served", () => {
    vi.useFakeTimers();
    const key = earlyKey("stale", 2);
    startEarly(key, () => Promise.resolve("old"));
    vi.advanceTimersByTime(16_000);
    expect(takeEarly(key)).toBeNull();
  });

  it("a rejected early call reaches its owner, not an unhandled rejection", async () => {
    const key = earlyKey("fail", 3);
    startEarly(key, () => Promise.reject(new Error("boom")));
    await expect(takeEarly(key)).rejects.toThrow("boom");
  });
});
