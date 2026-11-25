import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const api = vi.hoisted(() => ({
  getTeamStatus: vi.fn(),
  getDailyHours: vi.fn(),
  getWeeklySummary: vi.fn(),
  exportTimesheet: vi.fn(),
  isAdmin: () => true,
}));
const router = vi.hoisted(() => ({
  push: vi.fn(),
  back: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useSessionState", () => ({
  useSessionState: () => "authenticated",
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
}));

import AdminPage from "./page";

const HOURS = [
  {
    user_id: "synthetic-user",
    email: "synthetic@example.test",
    date: "2026-11-21",
    clock_in: "2026-11-21T01:00:00Z",
    clock_out: "2026-11-21T09:00:00Z",
    hours_worked: 8,
  },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("admin data loading errors", () => {
  beforeEach(() => {
    api.getTeamStatus.mockReset().mockResolvedValue([]);
    api.getDailyHours.mockReset().mockResolvedValue([]);
    api.getWeeklySummary.mockReset().mockResolvedValue([]);
    api.exportTimesheet.mockReset().mockResolvedValue(new Blob(["csv"]));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function renderLoadedPage() {
    render(<AdminPage />);
    await waitFor(() => expect(api.getDailyHours).toHaveBeenCalledTimes(1));
  }

  it.each([
    ["getDailyHours", "daily hours failed"],
    ["getTeamStatus", "team status failed"],
    ["getWeeklySummary", "weekly summary failed"],
  ] as const)(
    "shows an error banner when %s rejects",
    async (method, message) => {
      api[method].mockRejectedValueOnce(new Error(message));

      await renderLoadedPage();

      expect(await screen.findByText(message)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /daily hours/i }));
      await screen.findByLabelText("Select date");
      expect(screen.getByText("No records for this date")).toBeInTheDocument();
    },
  );

  it("shows rows without an error when all loaders resolve", async () => {
    api.getDailyHours.mockResolvedValue(HOURS);

    await renderLoadedPage();

    expect(screen.queryByText(/failed$/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /daily hours/i }));
    await screen.findByLabelText("Select date");
    expect(
      await screen.findByText("synthetic@example.test"),
    ).toBeInTheDocument();
  });

  it("loads a changed date once after the initial load", async () => {
    await renderLoadedPage();
    fireEvent.click(screen.getByRole("button", { name: /daily hours/i }));
    const input = await screen.findByLabelText("Select date");
    fireEvent.change(input, {
      target: { value: "2026-11-22" },
    });

    await waitFor(() =>
      expect(api.getDailyHours).toHaveBeenCalledWith("2026-11-22"),
    );
    expect(api.getDailyHours).toHaveBeenCalledTimes(2);
  });

  it("clears previous rows when a changed date fails", async () => {
    api.getDailyHours
      .mockResolvedValueOnce(HOURS)
      .mockRejectedValueOnce(new Error("new day failed"));

    await renderLoadedPage();
    fireEvent.click(screen.getByRole("button", { name: /daily hours/i }));
    await screen.findByLabelText("Select date");
    expect(
      await screen.findByText("synthetic@example.test"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Select date"), {
      target: { value: "2026-11-22" },
    });

    expect(await screen.findByText("new day failed")).toBeInTheDocument();
    expect(
      screen.queryByText("synthetic@example.test"),
    ).not.toBeInTheDocument();
  });

  it("keeps rows from the last selected date when responses arrive in reverse order", async () => {
    const first = deferred<typeof HOURS>();
    const second = deferred<typeof HOURS>();
    api.getTeamStatus
      .mockResolvedValueOnce([])
      .mockRejectedValue(new Error("team reload failed"));
    api.getDailyHours
      .mockResolvedValueOnce([])
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    await renderLoadedPage();
    fireEvent.click(screen.getByRole("button", { name: /daily hours/i }));
    const input = await screen.findByLabelText("Select date");
    fireEvent.change(input, { target: { value: "2026-11-22" } });
    await waitFor(() => expect(api.getDailyHours).toHaveBeenCalledTimes(2));
    const nextInput = await screen.findByLabelText("Select date");
    await waitFor(() => expect(nextInput).toHaveValue("2026-11-22"));
    fireEvent.change(nextInput, {
      target: { value: "2026-11-23" },
    });
    await waitFor(() => expect(api.getDailyHours).toHaveBeenCalledTimes(3));

    second.resolve([
      { ...HOURS[0], email: "last@example.test", date: "2026-11-23" },
    ]);
    expect(await screen.findByText("last@example.test")).toBeInTheDocument();
    first.resolve([
      { ...HOURS[0], email: "older@example.test", date: "2026-11-22" },
    ]);

    await waitFor(() =>
      expect(screen.queryByText("older@example.test")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("last@example.test")).toBeInTheDocument();
  });
});
