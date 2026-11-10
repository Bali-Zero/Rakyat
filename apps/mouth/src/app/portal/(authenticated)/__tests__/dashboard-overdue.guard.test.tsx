/**
 * Guard: a late filing must never read as "All caught up".
 *
 * Portal audit (10 Nov 2026): LKPM Q3 2026 was due 15 Oct and not filed, yet
 * the dashboard said "All caught up" because the summary only looked at the
 * next 30 days. Synthetic data only.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PortalHomePage from "../page";
import { collectOverdue, daysLate } from "../_lib/overdue";

const mocks = vi.hoisted(() => ({
  getDashboard: vi.fn(),
  getDashboardSummary: vi.fn(),
  getTimeline: vi.fn(),
  get: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: mocks.get,
    portal: {
      getDashboard: mocks.getDashboard,
      getDashboardSummary: mocks.getDashboardSummary,
      getTimeline: mocks.getTimeline,
    },
  },
}));

// 10 Nov 2026, 14:00 WITA
const NOW = new Date(2026, 10, 10, 14, 0, 0);

const Q3_2026 = {
  quarter: "Q3",
  year: 2026,
  deadline: "2026-10-15T00:00:00+00:00",
  days_remaining: -27,
  is_overdue: true,
};

const Q2_FILED = {
  id: 1,
  quarter: "Q2",
  year: 2026,
  status: "submitted" as const,
  realized_total: 0,
  oss_submitted: true,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-20T00:00:00Z",
};

function lkpmApi(history: unknown[], deadlines: unknown[]) {
  mocks.get.mockImplementation(async (url: string) =>
    url.includes("/history/me")
      ? { success: true, items: history }
      : { success: true, deadlines },
  );
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PortalHomePage />
    </QueryClientProvider>,
  );
}

describe("dashboard summary — overdue items", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    mocks.getDashboard.mockResolvedValue({
      visa: {
        status: "none",
        type: null,
        expiryDate: null,
        daysRemaining: null,
      },
      company: { status: "none", primaryCompanyName: null, totalCompanies: 0 },
      taxes: { status: "none", nextDeadline: null, daysToDeadline: null },
      documents: { total: 0, pending: 0 },
      messages: { unread: 0 },
      actions: [],
    });
    mocks.getTimeline.mockResolvedValue({ entries: [] });
    mocks.getDashboardSummary.mockResolvedValue({
      open_actions: [],
      upcoming_deadlines: [],
      unread_messages: 0,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("1 overdue LKPM + 0 upcoming is NOT 'All caught up'", async () => {
    lkpmApi([Q2_FILED], [Q3_2026]);
    renderPage();

    expect(
      await screen.findByRole("heading", {
        name: "1 action needed — LKPM Q3 2026 overdue",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("26 days late")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /LKPM Q3 2026/ })).toHaveAttribute(
      "href",
      "/portal/lkpm",
    );
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });

  it("control: 0 overdue + 0 upcoming is 'All caught up'", async () => {
    lkpmApi([Q2_FILED], []);
    renderPage();

    expect(await screen.findByText("All caught up")).toBeInTheDocument();
    expect(screen.queryByText(/action needed/)).not.toBeInTheDocument();
  });

  it("an overdue quarter that is already filed does not count", () => {
    const filedQ3 = { ...Q2_FILED, id: 2, quarter: "Q3" };
    expect(
      collectOverdue({
        summaryDeadlines: [],
        lkpmDeadlines: [Q3_2026],
        lkpmHistory: [filedQ3],
        now: NOW,
      }),
    ).toEqual([]);
  });

  it("no LKPM reports on file means no LKPM obligation shown", () => {
    expect(
      collectOverdue({
        summaryDeadlines: [],
        lkpmDeadlines: [Q3_2026],
        lkpmHistory: [],
        now: NOW,
      }),
    ).toEqual([]);
  });

  it("past summary deadlines are overdue, worst first, with no lookback limit", () => {
    const items = collectOverdue({
      summaryDeadlines: [
        {
          id: "visa-1",
          label: "Visa expiry",
          due_date: "2026-11-01",
          kind: "visa",
        },
        {
          id: "passport-1",
          label: "Passport expiry",
          due_date: "2025-01-01",
          kind: "passport",
        },
        {
          id: "practice-9",
          label: "Upcoming",
          due_date: "2026-11-20",
          kind: null,
        },
      ],
      lkpmDeadlines: [],
      lkpmHistory: [],
      now: NOW,
    });
    expect(items.map((i) => i.id)).toEqual(["passport-1", "visa-1"]);
  });

  it("counts calendar days: 15 Oct → 10 Nov is 26, not 27", () => {
    expect(daysLate("2026-10-15T00:00:00+00:00", NOW)).toBe(26);
    expect(daysLate("2026-11-10", NOW)).toBe(0);
  });
});
