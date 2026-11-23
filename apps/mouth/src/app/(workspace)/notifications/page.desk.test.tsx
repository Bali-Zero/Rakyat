import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

const requestMock = vi.hoisted(() => vi.fn());
const toasts = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api", () => ({ api: { crm: { request: requestMock } } }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toasts }));
vi.mock("sonner", () => ({ toast: vi.fn() }));

import NotificationsDashboardPage from "./page";

function dashboard(
  alerts: ReturnType<typeof alert>[] = [],
  systemStatus = "healthy",
) {
  return {
    stats: {
      total_alerts_24h: alerts.length,
      total_alerts_7d: alerts.length,
      total_alerts_30d: alerts.length,
      pending_count: 0,
      sent_count_24h: 0,
      failed_count_24h: 0,
      alerts_by_type: {},
      alerts_by_status: {},
      top_clients: [],
    },
    recent_alerts: alerts,
    system_status: systemStatus,
  };
}

function alert(id: number, alertType: string) {
  return {
    id,
    client_id: id,
    client_name: `Fixture Client ${id}`,
    client_email: `client-${id}@example.test`,
    alert_type: alertType,
    status: "pending",
    email_subject: `Fixture alert ${id}`,
    created_at: "2026-11-24T00:00:00Z",
    sent_at: null,
    error_message: null,
  };
}

describe("NotificationsDashboardPage — desk status and refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    requestMock.mockResolvedValue(dashboard());
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("marks emergency and expired alerts as Critical", async () => {
    requestMock.mockResolvedValue(
      dashboard([
        alert(1, "visa_emergency"),
        alert(2, "visa_expired"),
        alert(3, "passport_expired"),
        alert(4, "visa_warning"),
        alert(5, "birthday"),
        alert(6, "visa_critical"),
      ]),
    );

    render(<NotificationsDashboardPage />);

    expect(await screen.findAllByText("Critical")).toHaveLength(8);
  });

  it("keeps the established warning, birthday, and critical severities", async () => {
    requestMock.mockResolvedValue(
      dashboard([
        alert(1, "visa_warning"),
        alert(2, "birthday"),
        alert(3, "visa_critical"),
      ]),
    );

    render(<NotificationsDashboardPage />);

    expect(await screen.findAllByText("High")).toHaveLength(2);
    expect(screen.getAllByText("Low")).toHaveLength(2);
    expect(screen.getAllByText("Critical")).toHaveLength(2);
  });

  it("describes a backlogged system as a pending alerts backlog", async () => {
    requestMock.mockResolvedValue(dashboard([], "backlogged"));

    render(<NotificationsDashboardPage />);

    expect(
      await screen.findByText("Pending alerts backlog — processing."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Status unknown.")).not.toBeInTheDocument();
  });

  it("keeps the fallback copy for an unknown system status", async () => {
    requestMock.mockResolvedValue(dashboard([], "fixture_unknown"));

    render(<NotificationsDashboardPage />);

    expect(await screen.findByText("Status unknown.")).toBeInTheDocument();
  });

  it("keeps the initial-load spinner while its first dashboard request is pending", () => {
    requestMock.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<NotificationsDashboardPage />);

    expect(container.querySelector("svg.animate-spin")).toBeInTheDocument();
  });

  it("refreshes a poll without unmounting the focused search box", async () => {
    vi.useFakeTimers();
    let failPoll!: (reason: Error) => void;
    requestMock.mockResolvedValueOnce(dashboard()).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        failPoll = reject;
      }),
    );

    const { container } = render(<NotificationsDashboardPage />);
    await act(async () => {});
    const search = screen.getByRole("searchbox", {
      name: "Search notifications",
    });
    fireEvent.change(search, { target: { value: "fixture" } });
    search.focus();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });

    expect(requestMock).toHaveBeenCalledTimes(2);
    expect(container.querySelector("svg.animate-spin")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(search);

    await act(async () => {
      failPoll(new Error("fixture poll failure"));
    });
    expect(toasts.error).toHaveBeenCalledWith(
      "Failed to load dashboard",
      "Please try again",
    );
    expect(
      screen.getByRole("searchbox", { name: "Search notifications" }),
    ).toHaveValue("fixture");
    expect(document.activeElement).toBe(search);
    expect(container.querySelector("svg.animate-spin")).not.toBeInTheDocument();
  });
});
