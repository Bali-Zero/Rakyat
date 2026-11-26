import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import IntelligenceAnalyticsPage from "./page";
import {
  intelligenceApi,
  type IntelligenceAnalytics,
} from "@/lib/api/intelligence.api";

const toastError = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/intelligence.api");
vi.mock("@/lib/logger");
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: toastError, success: vi.fn() }),
}));
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const SelectContext = React.createContext<{
    onValueChange: (value: string) => void;
  } | null>(null);
  return {
    Select: ({
      onValueChange,
      children,
    }: {
      onValueChange: (value: string) => void;
      children: React.ReactNode;
    }) => (
      <SelectContext.Provider value={{ onValueChange }}>
        {children}
      </SelectContext.Provider>
    ),
    SelectTrigger: () => null,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: React.ReactNode }) => (
      <>{children}</>
    ),
    SelectItem: ({
      value,
      children,
    }: {
      value: string;
      children: React.ReactNode;
    }) => {
      const context = React.useContext(SelectContext);
      return (
        <button type="button" onClick={() => context?.onValueChange(value)}>
          {children}
        </button>
      );
    },
  };
});

function analytics(total: number, date = "2026-11-21"): IntelligenceAnalytics {
  return {
    period_days: total,
    summary: {
      total_processed: total,
      total_approved: total - 1,
      total_rejected: 1,
      total_published: total - 2,
      approval_rate: 90,
      rejection_rate: 10,
    },
    daily_trends: [
      {
        date,
        processed: total,
        approved: total - 1,
        rejected: 1,
        published: total - 2,
      },
    ],
    type_breakdown: {
      visa: { processed: total, approved: total - 1, rejected: 1 },
      news: { processed: 0, approved: 0, rejected: 0, published: 0 },
    },
    detection_type_breakdown: { NEW: total, UPDATED: 0 },
  };
}

function held() {
  let resolve!: (value: IntelligenceAnalytics) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<IntelligenceAnalytics>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderTwice() {
  return render(
    <StrictMode>
      <IntelligenceAnalyticsPage />
    </StrictMode>,
  );
}

describe("IntelligenceAnalyticsPage, one period on screen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // The selector and Refresh are replaced by the loading view while a
  // request is in flight, so two requests overlap only when the mount effect
  // runs twice (StrictMode); these tests use that to order the answers.
  it("keeps the newest answer when an older one arrives after it", async () => {
    const older = held();
    vi.mocked(intelligenceApi.getAnalytics)
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce(analytics(7));
    renderTwice();
    expect(await screen.findByText("6 approved")).toBeInTheDocument();

    await act(async () => {
      older.resolve(analytics(90));
    });
    expect(screen.getByText("6 approved")).toBeInTheDocument();
    expect(screen.queryByText("89 approved")).not.toBeInTheDocument();
  });

  it("ignores an older failure that arrives after the newest answer", async () => {
    const older = held();
    vi.mocked(intelligenceApi.getAnalytics)
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce(analytics(7));
    renderTwice();
    expect(await screen.findByText("6 approved")).toBeInTheDocument();

    await act(async () => {
      older.reject(new Error("fixture stale failure"));
    });
    expect(screen.getByText("6 approved")).toBeInTheDocument();
    expect(screen.queryByText("Analytics Unavailable")).not.toBeInTheDocument();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("stays loading when an older answer lands while the newest is in flight", async () => {
    const older = held();
    const newest = held();
    vi.mocked(intelligenceApi.getAnalytics)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newest.promise);
    renderTwice();
    await waitFor(() =>
      expect(intelligenceApi.getAnalytics).toHaveBeenCalledTimes(2),
    );

    await act(async () => {
      older.resolve(analytics(90));
    });
    expect(screen.getByText("Loading Analytics...")).toBeInTheDocument();
    expect(screen.queryByText("89 approved")).not.toBeInTheDocument();

    await act(async () => {
      newest.resolve(analytics(7));
    });
    expect(screen.getByText("6 approved")).toBeInTheDocument();
  });

  it("shows the error and none of the 30-day figures when a switch to 90 days fails", async () => {
    vi.mocked(intelligenceApi.getAnalytics)
      .mockResolvedValueOnce(analytics(30))
      .mockRejectedValueOnce(new Error("fixture failure"));
    const user = userEvent.setup();
    render(<IntelligenceAnalyticsPage />);
    expect(await screen.findByText("29 approved")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Last 90 days" }));

    expect(
      await screen.findByText("Analytics Unavailable"),
    ).toBeInTheDocument();
    expect(intelligenceApi.getAnalytics).toHaveBeenLastCalledWith(90);
    expect(screen.queryByText("29 approved")).not.toBeInTheDocument();
    expect(toastError).toHaveBeenCalled();
  });

  it("labels a trend bucket with its own calendar day west of UTC", async () => {
    const real = Date.prototype.toLocaleDateString;
    vi.spyOn(Date.prototype, "toLocaleDateString").mockImplementation(function (
      this: Date,
      locales,
      options,
    ) {
      return real.call(this, locales, {
        timeZone: "America/New_York",
        ...options,
      });
    });
    vi.mocked(intelligenceApi.getAnalytics).mockResolvedValue(
      analytics(12, "2026-11-21"),
    );
    render(<IntelligenceAnalyticsPage />);

    expect(await screen.findByText("Nov 21")).toBeInTheDocument();
    expect(screen.queryByText("Nov 20")).not.toBeInTheDocument();
  });

  it("still renders one ordinary load", async () => {
    vi.mocked(intelligenceApi.getAnalytics).mockResolvedValue(analytics(12));
    render(<IntelligenceAnalyticsPage />);
    expect(await screen.findByText("11 approved")).toBeInTheDocument();
    expect(screen.getByText("Nov 21")).toBeInTheDocument();
    expect(intelligenceApi.getAnalytics).toHaveBeenCalledWith(30);
  });
});
