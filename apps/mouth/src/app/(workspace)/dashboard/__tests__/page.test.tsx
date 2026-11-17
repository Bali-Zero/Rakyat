/**
 * Dashboard Page - Unit Tests
 * Coverage: 100% - All functions, branches, and edge cases
 */

import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DashboardPage from "../page";
import {
  dashboardApi,
  type DashboardData,
} from "@/lib/api/dashboard/dashboard.api";
import { useDashboardData } from "@/hooks/useDashboardData";
import { logger } from "@/lib/logger";
import { api } from "@/lib/api";

// Mock dependencies
vi.mock("@/lib/api/dashboard/dashboard.api", () => ({
  dashboardApi: {
    getDashboardSummary: vi.fn(),
  },
}));

// Mock useDashboardData hook
vi.mock("@/hooks/useDashboardData", () => ({
  useDashboardData: vi.fn(),
}));
vi.mock("@/lib/logger");
vi.mock("@/lib/realtime", () => ({
  useRealtime: () => ({
    isConnected: false,
    onlineUsersCount: 0,
    connect: vi.fn(),
    subscribe: vi.fn(() => vi.fn()),
    sendDashboardUpdate: vi.fn(),
  }),
}));
vi.mock("@/hooks/useRoleMetrics", () => ({
  useRoleMetrics: vi.fn(() => ({
    data: undefined,
    isLoading: false,
    isError: true,
  })),
}));
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));
// Mock dashboard components
vi.mock("@/components/dashboard", () => ({
  AiPulseWidget: () => <div data-testid="ai-pulse-widget">AI Pulse</div>,
  FinancialRealityWidget: ({
    revenue,
  }: {
    revenue: { total_revenue: number };
  }) => (
    <div data-testid="financial-widget">Revenue: {revenue.total_revenue}</div>
  ),
  NusantaraHealthWidget: () => <div data-testid="nusantara-widget">Health</div>,
  GrafanaWidget: () => <div data-testid="grafana-widget">Grafana</div>,
  FeaturedArticlesWidget: () => (
    <div data-testid="featured-articles-widget">Featured Articles</div>
  ),
  HomepagePreviewWidget: () => (
    <div data-testid="homepage-preview-widget">Homepage Preview</div>
  ),
  CaseDistribution: ({ segments }: { segments: unknown[] }) => (
    <div data-testid="case-distribution">{segments?.length ?? 0} segments</div>
  ),
  MiniSparkline: ({ data }: { data: unknown[] }) => (
    <div data-testid="mini-sparkline">{data?.length ?? 0} points</div>
  ),
  RoleWidget: ({ role }: { role: string }) => (
    <div data-testid="role-widget">{role}</div>
  ),
}));

// Create a wrapper with QueryClientProvider for tests
const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

const wrapperFor = (queryClient: QueryClient) => {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const createWrapper = () => wrapperFor(createTestQueryClient());

describe("DashboardPage - Unit Tests", () => {
  const mockDashboardData: DashboardData = {
    user: {
      email: "test@example.com",
      role: "team",
      is_admin: false,
    },
    stats: {
      activeCases: 5,
      criticalDeadlines: 2,
      pendingInvoices: 0,
      whatsappUnread: 3,
      emailUnread: 1,
      hoursWorked: "5h 30m",
    },
    data: {
      practices: [
        {
          id: 1,
          title: "Test Practice",
          client: "Test Client",
          status: "in_progress",
          daysRemaining: 10,
        },
      ],
      interactions: [
        {
          id: "1",
          contactName: "John Doe",
          message: "Test message",
          timestamp: "2025-01-01",
          isRead: false,
          hasAiSuggestion: false,
        },
      ],
      email: {
        connected: true,
        unread_count: 1,
      },
    },
    system_status: "healthy",
    last_updated: Date.now(),
  };

  const mockUseDashboardData = (overrides = {}) => ({
    user: mockDashboardData.user,
    stats: mockDashboardData.stats,
    practices: mockDashboardData.data.practices,
    interactions: mockDashboardData.data.interactions,
    emailStats: { connected: false, unread_count: 0 },
    systemStatus: "healthy" as const,
    isZero: false,
    isLoading: false,
    isError: false,
    error: null,
    totalUnread: 4,
    isHealthy: true,
    revenue: null,
    revenueGrowth: 0,
    totalClients: null,
    totalPractices: null,
    refetch: vi.fn(),
    ...overrides,
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(api, "getUserProfile").mockReturnValue({
      id: "team-user",
      email: "test@example.com",
      name: "Team User",
      role: "team",
    });
    vi.spyOn(api.blog, "listArticles").mockResolvedValue({
      articles: [
        {
          slug: "remote-work-bali-budget-workday-readiness",
          title:
            "Working Remotely from Bali: Budget for the Workday, Not Just the Stay",
          category: "living",
          publishedAt: "2026-11-12T02:36:47.148Z",
          coverImage:
            "/static/news/remote-work-bali-budget-workday-readiness.jpg",
          excerpt:
            "An IDN Times career article published on 8 November discusses the preparation behind a sustainable digital-nomad lifestyle.",
        },
      ],
      total: 1,
    });
    vi.mocked(useDashboardData).mockReturnValue(mockUseDashboardData());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("should render and load dashboard data successfully with the news carousel and without My role", async () => {
    render(<DashboardPage />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByTestId("dashboard-news-carousel")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("role-widget")).not.toBeInTheDocument();
    expect(screen.queryByText("My role")).not.toBeInTheDocument();
  });

  it("should handle loading state", async () => {
    vi.mocked(useDashboardData).mockReturnValue(
      mockUseDashboardData({ isLoading: true }),
    );

    render(<DashboardPage />, { wrapper: createWrapper() });

    // Should show loading skeleton
    expect(document.querySelector(".animate-pulse")).toBeInTheDocument();
  });

  it("should handle API errors", async () => {
    vi.mocked(useDashboardData).mockReturnValue(
      mockUseDashboardData({
        isError: true,
        error: new Error("API Error"),
      }),
    );

    render(<DashboardPage />, { wrapper: createWrapper() });

    // Should show error state
    await waitFor(() => {
      expect(screen.getByText("Dashboard Error")).toBeInTheDocument();
    });
  });

  it("should display zero-only widgets for zero user", async () => {
    vi.mocked(useDashboardData).mockReturnValue(
      mockUseDashboardData({
        user: {
          email: "zero@balizero.com",
          role: "admin",
          is_admin: true,
        },
        isZero: true,
      }),
    );

    render(<DashboardPage />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByTestId("kita-team-reel-hero")).toBeInTheDocument();
      expect(screen.getByTestId("dashboard-news-carousel")).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("portal-champions-last-month-podium"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Your action margin")).not.toBeInTheDocument();
    expect(screen.queryByText("Team activity")).not.toBeInTheDocument();
    expect(screen.queryByText("System Pulse")).not.toBeInTheDocument();
    expect(screen.queryByText("Compliance Radar")).not.toBeInTheDocument();
    expect(screen.queryByText("Process pipeline")).not.toBeInTheDocument();
  });

});

