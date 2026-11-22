import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// "Retry failed" asks the server to re-send every failed alert of the last 7
// days to its client. The button stayed live while that request ran, so a
// second click sent the same mails again, and its label promised the 24-hour
// count instead.
const requestMock = vi.hoisted(() => vi.fn());
const toasts = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api", () => ({ api: { crm: { request: requestMock } } }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toasts }));
vi.mock("sonner", () => ({ toast: vi.fn() }));

import NotificationsDashboardPage from "./page";

const DASHBOARD = "/api/admin/notifications/dashboard";
const RETRY = "/api/admin/notifications/retry";

function dashboard(failedIn24h: number) {
  return {
    stats: {
      total_alerts_24h: 5,
      total_alerts_7d: 20,
      total_alerts_30d: 40,
      pending_count: 0,
      sent_count_24h: 2,
      failed_count_24h: failedIn24h,
      alerts_by_type: {},
      alerts_by_status: {},
      top_clients: [],
    },
    recent_alerts: [],
    system_status: "healthy",
  };
}

type Answer = { message: string };

/** A retry request the test settles by hand, one per call. */
function pendingRetries() {
  const settle: Array<{
    resolve: (answer: Answer) => void;
    reject: (reason: Error) => void;
  }> = [];
  const promise = () =>
    new Promise<Answer>((resolve, reject) => settle.push({ resolve, reject }));
  return { settle, promise };
}

function callsTo(url: string) {
  return requestMock.mock.calls.filter((call) => call[0] === url);
}

describe("retrying the failed notifications", () => {
  let retries: ReturnType<typeof pendingRetries>;
  let failedIn24h: number;

  beforeEach(() => {
    vi.clearAllMocks();
    retries = pendingRetries();
    failedIn24h = 3;
    requestMock.mockImplementation((url: string) =>
      url === RETRY
        ? retries.promise()
        : Promise.resolve(dashboard(failedIn24h)),
    );
  });

  it("sends one request for two clicks while the first is still running", async () => {
    render(<NotificationsDashboardPage />);
    const button = await screen.findByRole("button", { name: /retry failed/i });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(callsTo(RETRY)).toEqual([[RETRY, { method: "POST", body: "{}" }]]);
    const busy = await screen.findByRole("button", { name: "Retrying…" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    fireEvent.click(busy);
    expect(callsTo(RETRY)).toHaveLength(1);

    failedIn24h = 1;
    retries.settle[0].resolve({ message: "Retried 3 alerts, 2 successful" });

    await waitFor(() =>
      expect(toasts.success).toHaveBeenCalledWith(
        "Retry completed",
        "Retried 3 alerts, 2 successful",
      ),
    );
    // The list is read again, and the button is live for what is still failed.
    await waitFor(() => expect(callsTo(DASHBOARD)).toHaveLength(2));
    const again = await screen.findByRole("button", { name: /retry failed/i });
    expect(again).toBeEnabled();
    fireEvent.click(again);
    expect(callsTo(RETRY)).toHaveLength(2);
  });

  it("names the scope the server retries, not the 24-hour count", async () => {
    render(<NotificationsDashboardPage />);

    const button = await screen.findByRole("button", { name: /retry failed/i });

    expect(button).toHaveAccessibleName("Retry failed · 7 days");
    expect(button).toHaveAttribute(
      "title",
      "Re-sends every failed alert of the last 7 days",
    );
  });

  it("frees the button when the request fails, and says the server may still be sending", async () => {
    render(<NotificationsDashboardPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /retry failed/i }),
    );

    retries.settle[0].reject(new Error("timeout"));

    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith(
        "Retry failed",
        "The server may still be sending. Refresh and check the failed count before retrying.",
      ),
    );
    const button = await screen.findByRole("button", { name: /retry failed/i });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(callsTo(RETRY)).toHaveLength(2);
  });

  it("is not offered when nothing failed in the last 24 hours", async () => {
    failedIn24h = 0;
    render(<NotificationsDashboardPage />);

    await waitFor(() => expect(callsTo(DASHBOARD)).toHaveLength(1));
    await screen.findByText("Failed · 24h");
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});
