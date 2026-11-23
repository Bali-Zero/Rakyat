import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkspaceLayout from "./layout";

// The gate wall receives the layout's admin decision as a prop; the wall
// itself is replaced so the test reads that decision and nothing else.
const { mockGetGateStatus, mockGetUserProfile } = vi.hoisted(() => ({
  mockGetGateStatus: vi.fn(),
  mockGetUserProfile: vi.fn(),
}));

vi.mock("./GateScreen", () => ({
  GateScreen: ({ isAdmin }: { isAdmin: boolean }) => (
    <div>{isAdmin ? "override offered" : "no override"}</div>
  ),
  default: ({ isAdmin }: { isAdmin: boolean }) => (
    <div>{isAdmin ? "override offered" : "no override"}</div>
  ),
}));

vi.mock("@/lib/api", async () => {
  const { ApiError } = await vi.importActual<
    typeof import("@/lib/api/error-handler")
  >("@/lib/api/error-handler");
  return {
    ApiError,
    api: {
      getUserProfile: mockGetUserProfile,
      getProfile: vi.fn(),
      getGateStatus: mockGetGateStatus,
      hasSession: vi.fn().mockResolvedValue("authenticated"),
      logout: vi.fn(),
      isAdmin: () => false,
    },
  };
});
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/dashboard",
}));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ removeQueries: vi.fn() }),
}));
vi.mock("@/components/workspace/AppSidebar", () => ({
  AppSidebar: () => <aside aria-label="Primary" />,
}));
vi.mock("@/components/workspace/Header", () => ({ Header: () => <header /> }));
vi.mock("@/components/workspace/ZantaraWidget", () => ({
  ZantaraWidget: () => null,
}));
vi.mock("@/components/workspace/KitaCommandPalette", () => ({
  KitaCommandPalette: () => null,
}));
vi.mock("@/components/ui/toast", () => ({
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/i18n", () => ({
  I18nProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/components/optimization", () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("./dashboard/VisaOracleTestingGuideOverlay", () => ({
  VisaOracleTestingGuideOverlay: () => null,
}));
vi.mock("@/hooks/useDashboardData", () => ({
  removeDashboardQueries: vi.fn(),
}));

describe("WorkspaceLayout gate admin override", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    delete (window as { location?: Location }).location;
    (window as unknown as { location: Partial<Location> }).location = {
      hostname: "kita.balizero.com",
      pathname: "/dashboard",
      href: "https://kita.balizero.com/dashboard",
      replace: vi.fn(),
    };
    mockGetGateStatus.mockResolvedValue({
      blocked: true,
      sections: {
        documents: { count: 1, blocking: true },
        late_note: { count: 0, blocking: false },
        deadlines: { count: 0, blocking: false },
      },
      as_of: "2026-11-23T00:00:00Z",
    });
  });

  afterEach(() => {
    delete (window as { location?: Location }).location;
    (window as unknown as { location: Location }).location = originalLocation;
    vi.clearAllMocks();
  });

  it.each([
    ["CEO", "override offered"],
    ["Board Member", "override offered"],
    ["admin", "override offered"],
    ["staff", "no override"],
  ])("a %s blocked by the gate sees: %s", async (role, expected) => {
    mockGetUserProfile.mockReturnValue({
      name: "Synthetic Member",
      email: "member@example.test",
      role,
      team: "Management",
    });

    render(
      <WorkspaceLayout>
        <div>Dashboard content</div>
      </WorkspaceLayout>,
    );

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.queryByText("Dashboard content")).not.toBeInTheDocument();
  });
});
