/**
 * Guard: a late filing must never read as "All caught up".
 *
 * Portal audit (10 Nov 2026): LKPM Q3 2026 was due 15 Oct and not filed, yet
 * the dashboard said "All caught up" because the summary only looked at the
 * next 30 days. Synthetic data only.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import React from "react";
import {
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import PortalHomePage from "../page";
import {
  collectOverdue,
  daysLate,
  lkpmReadState,
  usePortalLkpmHistory,
} from "../_lib/overdue";

const mocks = vi.hoisted(() => ({
  getDashboard: vi.fn(),
  getDashboardSummary: vi.fn(),
  getTimeline: vi.fn(),
  get: vi.fn(),
  getUserProfile: vi.fn(),
  getPortalImpersonation: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: mocks.get,
    getUserProfile: mocks.getUserProfile,
    getPortalImpersonation: mocks.getPortalImpersonation,
    portal: {
      getDashboard: mocks.getDashboard,
      getDashboardSummary: mocks.getDashboardSummary,
      getTimeline: mocks.getTimeline,
    },
  },
}));

// 10 Nov 2026, 14:00 WITA
const NOW = new Date(2026, 10, 10, 14, 0, 0);

const Q2_FILED = {
  id: 1,
  company_id: 101,
  company_name: "Synthetic Co A",
  quarter: "Q2",
  year: 2026,
  status: "submitted" as const,
  realized_total: 0,
  oss_submitted: true,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-20T00:00:00Z",
};

function lkpmApi(history: unknown[]) {
  mocks.get.mockResolvedValue({ success: true, items: history });
}

function renderPage(
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }),
) {
  return render(
    <QueryClientProvider client={client}>
      <PortalHomePage />
    </QueryClientProvider>,
  );
}

describe("dashboard summary — overdue items", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getUserProfile.mockReturnValue({ id: "acct-a" });
    mocks.getPortalImpersonation.mockReturnValue(null);
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
    lkpmApi([Q2_FILED]);
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
    lkpmApi([Q2_FILED, { ...Q2_FILED, id: 2, quarter: "Q3" }]);
    renderPage();

    expect(await screen.findByText("All caught up")).toBeInTheDocument();
    expect(screen.queryByText(/action needed/)).not.toBeInTheDocument();
  });

  it("an overdue quarter that is already filed does not count", () => {
    const filedQ3 = { ...Q2_FILED, id: 2, quarter: "Q3" };
    expect(
      collectOverdue({
        summaryDeadlines: [],
        lkpmHistory: [filedQ3],
        now: NOW,
      }),
    ).toEqual([]);
  });

  it("no LKPM reports on file means no LKPM obligation shown", () => {
    expect(
      collectOverdue({
        summaryDeadlines: [],
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
      lkpmHistory: [],
      now: NOW,
    });
    expect(items.map((i) => i.id)).toEqual(["passport-1", "visa-1"]);
  });

  it("no lookback limit: Q3 2026 still overdue on 20 Dec, before Q4 is due", () => {
    const items = collectOverdue({
      summaryDeadlines: [],
      lkpmHistory: [Q2_FILED],
      now: new Date(2026, 11, 20, 9, 0, 0),
    });
    expect(items).toEqual([
      {
        id: "lkpm-101-Q3-2026",
        label: "LKPM Q3 2026",
        daysLate: 66,
        href: "/portal/lkpm",
      },
    ]);
  });

  it("an approved-but-not-filed quarter is still overdue", () => {
    const approvedQ3 = {
      ...Q2_FILED,
      id: 3,
      quarter: "Q3",
      status: "approved" as const,
      oss_submitted: false,
    };
    expect(
      collectOverdue({
        summaryDeadlines: [],
        lkpmHistory: [Q2_FILED, approvedQ3],
        now: NOW,
      }).map((i) => i.id),
    ).toEqual(["lkpm-101-Q3-2026"]);
  });

  it("counts calendar days: 15 Oct → 10 Nov is 26, not 27", () => {
    expect(daysLate("2026-10-15T00:00:00+00:00", NOW)).toBe(26);
    expect(daysLate("2026-11-10", NOW)).toBe(0);
  });
});

const COMPANY_A = { company_id: 101, company_name: "Synthetic Co A" };
const COMPANY_B = { company_id: 202, company_name: "Synthetic Co B" };

describe("per-company filing", () => {
  it("one company filed, the other not: the unfiled one is overdue", () => {
    const items = collectOverdue({
      summaryDeadlines: [],
      lkpmHistory: [
        { ...Q2_FILED, id: 1, ...COMPANY_A },
        { ...Q2_FILED, id: 2, quarter: "Q3", ...COMPANY_A },
        { ...Q2_FILED, id: 3, ...COMPANY_B },
      ],
      now: NOW,
    });
    expect(items).toEqual([
      {
        id: "lkpm-202-Q3-2026",
        label: "LKPM Q3 2026 · Synthetic Co B",
        daysLate: 26,
        href: "/portal/lkpm",
      },
    ]);
  });

  it("every company filed: nothing overdue", () => {
    const items = collectOverdue({
      summaryDeadlines: [],
      lkpmHistory: [
        { ...Q2_FILED, id: 1, quarter: "Q3", ...COMPANY_A },
        { ...Q2_FILED, id: 2, quarter: "Q3", ...COMPANY_B },
      ],
      now: NOW,
    });
    expect(items).toEqual([]);
  });

  it("both companies late: one row each, never merged", () => {
    const items = collectOverdue({
      summaryDeadlines: [],
      lkpmHistory: [
        { ...Q2_FILED, id: 1, ...COMPANY_A },
        { ...Q2_FILED, id: 2, ...COMPANY_B },
      ],
      now: NOW,
    });
    expect(items.map((i) => i.id).sort()).toEqual([
      "lkpm-101-Q3-2026",
      "lkpm-202-Q3-2026",
    ]);
  });

  it("the dashboard names the late company in a multi-company page", async () => {
    mocks.get.mockResolvedValue({
      success: true,
      items: [
        { ...Q2_FILED, id: 1, quarter: "Q3", ...COMPANY_A },
        { ...Q2_FILED, id: 2, ...COMPANY_B },
      ],
    });
    renderPage();
    expect(
      await screen.findByRole("heading", {
        name: "1 action needed — LKPM Q3 2026 · Synthetic Co B overdue",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });
});

describe("LKPM history unreadable is never an all-clear", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getUserProfile.mockReturnValue({ id: "acct-a" });
    mocks.getPortalImpersonation.mockReturnValue(null);
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

  it("a failed load renders the error state, not 'All caught up'", async () => {
    mocks.get.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not check your LKPM filings/,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
    expect(screen.queryByText(/Nothing overdue/)).not.toBeInTheDocument();
  });

  it("a reply that is not a history (success false) is an error too", async () => {
    mocks.get.mockResolvedValue({ success: false });
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });

  it.each([
    ["bad quarter", { quarter: "Q9" }],
    ["missing year", { year: undefined }],
    ["missing company", { company_id: undefined }],
  ])(
    "a history row with a %s is an error, not a clean slate",
    async (_n, patch) => {
      mocks.get.mockResolvedValue({
        success: true,
        items: [{ ...Q2_FILED, id: 9, ...patch }],
      });
      renderPage();
      expect(await screen.findByRole("alert")).toBeInTheDocument();
      expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
    },
  );

  it.each([["false"], [1], [{}], [null]])(
    "a non-boolean success flag (%j) is an error, never a clean slate",
    async (success) => {
      mocks.get.mockResolvedValue({ success, items: [] });
      renderPage();
      expect(await screen.findByRole("alert")).toBeInTheDocument();
      expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
    },
  );

  it("a reply without items is an error, not an empty history", async () => {
    mocks.get.mockResolvedValue({ success: true });
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });

  it("while the history is still loading nothing claims all-clear", async () => {
    mocks.get.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });

  it("Retry reads again and recovers to the real answer", async () => {
    mocks.get.mockRejectedValueOnce(new Error("boom"));
    mocks.get.mockResolvedValue({
      success: true,
      items: [{ ...Q2_FILED, id: 2, quarter: "Q3" }],
    });
    renderPage();
    const retry = await screen.findByRole("button", { name: "Retry" });
    retry.click();
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
  });

  it("an error beside other content still shows the notice", async () => {
    mocks.get.mockRejectedValue(new Error("boom"));
    mocks.getDashboardSummary.mockResolvedValue({
      open_actions: [],
      upcoming_deadlines: [
        { id: "x", label: "Upcoming", due_date: "2026-11-20", kind: null },
      ],
      unread_messages: 0,
    });
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(await screen.findByText("Upcoming")).toBeInTheDocument();
  });
});

describe("LKPM read state transitions", () => {
  const history = [
    {
      ...Q2_FILED,
      company_id: 401,
      company_name: "PT Contoh Satu",
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getUserProfile.mockReturnValue({ id: "contoh-a" });
    mocks.getPortalImpersonation.mockReturnValue(null);
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
    onlineManager.setOnline(true);
  });

  it.each([
    ["pending fetching", undefined, false, true, "checking"],
    ["pending paused", undefined, false, false, "checking"],
    ["pending idle", undefined, false, false, "checking"],
    ["error idle", undefined, true, false, "error"],
    ["error fetching", undefined, true, true, "checking"],
    ["success", history, false, false, "ready"],
    ["error retained", history, true, false, "ready"],
    ["error fetching retained", history, true, true, "ready"],
  ])("lkpmReadState: %s", (_case, data, isError, isFetching, expected) => {
    expect(lkpmReadState({ data, isError, isFetching })).toBe(expected);
  });

  it("background failure keeps a retained history ready", () => {
    expect(
      lkpmReadState({
        data: history,
        isError: true,
        isFetching: false,
      }),
    ).toBe("ready");
  });

  it("checking with other content shows that content and a status line", async () => {
    mocks.get.mockReturnValue(new Promise(() => {}));
    mocks.getDashboardSummary.mockResolvedValue({
      open_actions: [],
      upcoming_deadlines: [
        {
          id: "contoh-upcoming",
          label: "Upcoming",
          due_date: "2026-11-20",
          kind: null,
        },
      ],
      unread_messages: 0,
    });
    renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    expect(await screen.findByText("Upcoming")).toBeInTheDocument();
    expect(screen.queryByText(/LKPM Q3 2026/)).not.toBeInTheDocument();
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
  });

  it("Retry held pending replaces the alert and two same-tick clicks join one request", async () => {
    let resolve: (value: unknown) => void = () => {};
    mocks.get.mockRejectedValueOnce(new Error("boom"));
    mocks.get.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    renderPage();
    const retry = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retry);
    fireEvent.click(retry);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledTimes(2);
    await act(async () => resolve({ success: true, items: history }));
    expect(
      await screen.findByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
  });

  it("offline initial load stays checking and resolves on reconnect", async () => {
    let resolve: (value: unknown) => void = () => {};
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(["portal", "dashboard"], {
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
    client.setQueryData(["portal", "dashboard", "summary"], {
      open_actions: [],
      upcoming_deadlines: [],
      unread_messages: 0,
    });
    client.setQueryData(["portal", "timeline", 20], { entries: [] });
    onlineManager.setOnline(false);
    mocks.get.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    renderPage(client);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
    await act(async () => resolve({ success: true, items: [] }));
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
  });

  it("an offline Retry stays checking and resolves on reconnect", async () => {
    let resolve: (value: unknown) => void = () => {};
    mocks.get.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    const retry = await screen.findByRole("button", { name: "Retry" });
    onlineManager.setOnline(false);
    mocks.get.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    fireEvent.click(retry);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
    await act(async () => resolve({ success: true, items: [] }));
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
  });

  it("a failed background refetch retains history through a further pending refetch", async () => {
    mocks.get.mockResolvedValueOnce({ success: true, items: history });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    renderPage(client);
    expect(
      await screen.findByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
    mocks.get.mockRejectedValueOnce(new Error("background"));
    await act(async () => {
      await client.refetchQueries({
        queryKey: ["portal", "dashboard", "lkpm-history"],
      });
    });
    expect(
      screen.getByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    mocks.get.mockReturnValueOnce(new Promise(() => {}));
    act(() => {
      void client.refetchQueries({
        queryKey: ["portal", "dashboard", "lkpm-history"],
      });
    });
    expect(
      screen.getByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
  });

  it("an uncached key shows checking, while returning to cached A restores only A", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false } },
    });
    mocks.get.mockResolvedValueOnce({ success: true, items: history });
    const view = renderPage(client);
    expect(
      await screen.findByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
    mocks.getUserProfile.mockReturnValue({ id: "contoh-b" });
    mocks.get.mockReturnValueOnce(new Promise(() => {}));
    view.rerender(
      <QueryClientProvider client={client}>
        <PortalHomePage />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Checking your filings",
    );
    expect(screen.queryByText("All caught up")).not.toBeInTheDocument();
    mocks.getUserProfile.mockReturnValue({ id: "contoh-a" });
    view.rerender(
      <QueryClientProvider client={client}>
        <PortalHomePage />
      </QueryClientProvider>,
    );
    expect(
      await screen.findByRole("heading", { name: /LKPM Q3 2026/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("returning to cached empty A shows A's all-clear immediately", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false } },
    });
    mocks.get.mockResolvedValueOnce({ success: true, items: [] });
    const view = renderPage(client);
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
    mocks.getUserProfile.mockReturnValue({ id: "contoh-b" });
    mocks.get.mockReturnValueOnce(new Promise(() => {}));
    view.rerender(
      <QueryClientProvider client={client}>
        <PortalHomePage />
      </QueryClientProvider>,
    );
    await screen.findByRole("status");
    mocks.getUserProfile.mockReturnValue({ id: "contoh-a" });
    view.rerender(
      <QueryClientProvider client={client}>
        <PortalHomePage />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("All caught up")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("two accounts in one browser session", () => {
  const A_HISTORY = [
    { ...Q2_FILED, id: 11, quarter: "Q3", ...COMPANY_A },
    { ...Q2_FILED, id: 12, quarter: "Q4", ...COMPANY_A },
  ];
  const B_HISTORY = [{ ...Q2_FILED, id: 21, ...COMPANY_B }];

  function HistoryProbe() {
    const q = usePortalLkpmHistory();
    return <div data-testid="probe">{JSON.stringify(q.data ?? null)}</div>;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getPortalImpersonation.mockReturnValue(null);
  });

  it("the cache key carries identity: B never reads A's history", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false } },
    });
    const wrap = (ui: React.ReactNode) => (
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    );

    mocks.getUserProfile.mockReturnValue({ id: "acct-a" });
    mocks.get.mockResolvedValue({ success: true, items: A_HISTORY });
    const first = render(wrap(<HistoryProbe />));
    await vi.waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toContain('"id":11'),
    );
    first.unmount();

    mocks.getUserProfile.mockReturnValue({ id: "acct-b" });
    mocks.get.mockResolvedValue({ success: true, items: B_HISTORY });
    render(wrap(<HistoryProbe />));
    await vi.waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toContain('"id":21'),
    );
    expect(screen.getByTestId("probe").textContent).not.toContain('"id":11');
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });

  it("a different impersonation target is a different cache entry", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false } },
    });
    const wrap = (ui: React.ReactNode) => (
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    );
    mocks.getUserProfile.mockReturnValue({ id: "staff" });

    mocks.getPortalImpersonation.mockReturnValue(7);
    mocks.get.mockResolvedValue({ success: true, items: A_HISTORY });
    render(wrap(<HistoryProbe />)).unmount();
    await vi.waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));

    mocks.getPortalImpersonation.mockReturnValue(8);
    mocks.get.mockResolvedValue({ success: true, items: B_HISTORY });
    render(wrap(<HistoryProbe />));
    await vi.waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toContain('"id":21'),
    );
  });
});
