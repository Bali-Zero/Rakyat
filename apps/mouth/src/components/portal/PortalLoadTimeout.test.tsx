import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  PORTAL_LOAD_TIMEOUT_MS,
  usePortalLoadTimeout,
} from "./PortalLoadTimeout";

/**
 * Portal skeletons had no time limit: a hung request (30 s client timeout,
 * then up to 3 retries) kept the page grey for minutes with nothing to press.
 * After PORTAL_LOAD_TIMEOUT_MS the page now says so and offers Retry.
 */

const { mockGetDashboard, mockGetSummary, mockGetTimeline, mockListMatters } =
  vi.hoisted(() => ({
    mockGetDashboard: vi.fn(),
    mockGetSummary: vi.fn(),
    mockGetTimeline: vi.fn(),
    mockListMatters: vi.fn(),
  }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({}),
}));

vi.mock("@/lib/api", () => ({
  api: {
    portal: {
      getDashboard: mockGetDashboard,
      getDashboardSummary: mockGetSummary,
      getTimeline: mockGetTimeline,
      listMatters: mockListMatters,
    },
  },
}));

import PortalHomePage from "@/app/portal/(authenticated)/page";
import PortalMattersPage from "@/app/portal/(authenticated)/matters/page";

const never = () => new Promise<never>(() => {});

function renderPage(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.clearAllMocks();
  mockGetDashboard.mockImplementation(never);
  mockGetSummary.mockImplementation(never);
  mockGetTimeline.mockImplementation(never);
  mockListMatters.mockImplementation(never);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("portal pages stop the skeleton after the load timeout", () => {
  it("dashboard: shows an error with Retry, and Retry fetches again", async () => {
    renderPage(<PortalHomePage />);
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();

    await act(async () => {
      vi.advanceTimersByTime(PORTAL_LOAD_TIMEOUT_MS);
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      /taking longer than expected/i,
    );
    const calls = mockGetDashboard.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await act(async () => {});
    expect(mockGetDashboard.mock.calls.length).toBeGreaterThan(calls);
    // A fresh window: the skeleton is back until the next timeout.
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("matters: shows an error with Retry after the timeout", async () => {
    renderPage(<PortalMattersPage />);
    await act(async () => {
      vi.advanceTimersByTime(PORTAL_LOAD_TIMEOUT_MS);
    });
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("innocence: a load that finishes in time never shows the timeout", async () => {
    mockListMatters.mockResolvedValue([]);
    renderPage(<PortalMattersPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PORTAL_LOAD_TIMEOUT_MS * 2);
    });
    expect(screen.queryByText(/taking longer than expected/i)).toBeNull();
  });
});

describe("usePortalLoadTimeout", () => {
  it("does not carry an old expiry into the next loading phase", () => {
    const client = new QueryClient();
    const { result, rerender } = renderHook(
      ({ loading }) => usePortalLoadTimeout(loading),
      {
        initialProps: { loading: true },
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );
    act(() => {
      vi.advanceTimersByTime(PORTAL_LOAD_TIMEOUT_MS);
    });
    expect(result.current.timedOut).toBe(true);

    rerender({ loading: false });
    expect(result.current.timedOut).toBe(false);
    rerender({ loading: true });
    expect(result.current.timedOut).toBe(false);
  });
});
