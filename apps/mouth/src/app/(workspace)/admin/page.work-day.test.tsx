import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// The timesheet's days are the team's civil days in Bali. The page took them
// from `toISOString()`, the UTC day: before 08:00 in Bali the daily view opened
// on yesterday and the export stopped at yesterday, and in a Bali browser the
// export began on the last day of the previous month.
const api = vi.hoisted(() => ({
  getTeamStatus: vi.fn(),
  getDailyHours: vi.fn(),
  getWeeklySummary: vi.fn(),
  exportTimesheet: vi.fn(),
  isAdmin: () => true,
}));

vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useSessionState", () => ({
  useSessionState: () => "authenticated",
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
}));

import AdminPage from "./page";

const CASES = [
  {
    when: "06:00 in Bali on the 21st, still the 20th in UTC",
    instant: "2026-11-20T22:00:00Z",
    today: "2026-11-21",
    monthStart: "2026-11-01",
  },
  {
    when: "01:00 in Bali on the first of the month",
    instant: "2026-10-31T17:00:00Z",
    today: "2026-11-01",
    monthStart: "2026-11-01",
  },
  {
    when: "15:00 in Bali",
    instant: "2026-11-21T07:00:00Z",
    today: "2026-11-21",
    monthStart: "2026-11-01",
  },
];

describe("the days of the admin timesheet", () => {
  let downloads: string[];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    api.getTeamStatus.mockReset().mockResolvedValue([]);
    api.getDailyHours.mockReset().mockResolvedValue([]);
    api.getWeeklySummary.mockReset().mockResolvedValue([]);
    api.exportTimesheet.mockReset().mockResolvedValue(new Blob(["csv"]));
    downloads = [];
    window.URL.createObjectURL = vi.fn(() => "blob:timesheet");
    window.URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each(CASES)(
    "at $when the daily view opens on $today",
    async ({ instant, today }) => {
      vi.setSystemTime(new Date(instant));

      render(<AdminPage />);

      await waitFor(() => expect(api.getDailyHours).toHaveBeenCalled());
      expect(api.getDailyHours.mock.calls.map((call) => call[0])).toEqual([
        today,
      ]);
    },
  );

  it.each(CASES)(
    "at $when the export runs from $monthStart to $today",
    async ({ instant, today, monthStart }) => {
      vi.setSystemTime(new Date(instant));
      render(<AdminPage />);
      const button = await screen.findByRole("button", { name: /export csv/i });

      fireEvent.click(button);

      await waitFor(() => expect(downloads).toHaveLength(1));
      expect(api.exportTimesheet.mock.calls).toEqual([[monthStart, today]]);
      expect(downloads).toEqual([`timesheet_${monthStart}_to_${today}.csv`]);
    },
  );
});
