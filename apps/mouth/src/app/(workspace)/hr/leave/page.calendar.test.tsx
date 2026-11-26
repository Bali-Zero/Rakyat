import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { LeaveRequest } from "@/types/hr";

const getProfile = vi.fn();
const listLeaveRequests = vi.fn();
const getCapabilities = vi.fn();
const member = {
  hr_admin: false,
  accounting_admin: false,
  has_employee_record: true,
};

vi.mock("@/lib/api", () => ({ api: { getProfile: () => getProfile() } }));
vi.mock("@/lib/api/hr/hr", () => ({
  getCapabilities: () => getCapabilities(),
  listLeaveRequests: () => listLeaveRequests(),
  getLeaveBalance: () => Promise.resolve({ balances: [] }),
  getTeamLeaveSummary: () => Promise.reject(new Error("403")),
  approveLeave: vi.fn(),
  rejectLeave: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const base: Omit<LeaveRequest, "status" | "start_date" | "end_date"> = {
  id: 1,
  employee_id: 10,
  employee_name: "Sample Person",
  employee_email: "sample@example.test",
  leave_type_id: 1,
  leave_type_name: "Annual",
  total_days: 1,
  reason: null,
  requested_at: "2026-11-09T00:00:00Z",
  reviewed_by: null,
  reviewed_at: null,
  rejection_reason: null,
  is_mine: true,
  can_review: false,
};

function westOfUtcLabels() {
  const real = Date.prototype.toLocaleDateString;
  vi.spyOn(Date.prototype, "toLocaleDateString").mockImplementation(function (
    this: Date,
    locales,
    options,
  ) {
    return real.call(this, locales, {
      timeZone: "America/Los_Angeles",
      ...options,
    });
  });
}

async function renderPage(
  requests: LeaveRequest[] = [],
  landmark = "My Requests",
) {
  listLeaveRequests.mockResolvedValue({ requests });
  const { default: LeavePage } = await import("./page");
  const result = render(<LeavePage />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText(landmark)).toBeInTheDocument();
  return result;
}

function calendar(container: HTMLElement) {
  return container.querySelector(".border.rounded-xl.p-4") as HTMLElement;
}

describe("leave calendar dates", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    getCapabilities.mockResolvedValue(member);
    getProfile.mockResolvedValue({
      email: "sample@example.test",
      role: "member",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("uses the Bali date for today", async () => {
    vi.setSystemTime(new Date("2026-11-24T23:30:00Z"));
    const { container } = await renderPage();
    const miniCalendar = calendar(container);

    expect(miniCalendar).toHaveTextContent("November 2026");
    expect(screen.getByText("25")).toHaveClass("font-bold");
    expect(screen.getByText("24")).not.toHaveClass("font-bold");
  });

  it("opens on the Bali month when Bali has already turned the month", async () => {
    vi.setSystemTime(new Date("2026-11-30T20:00:00Z"));
    const { container } = await renderPage();
    const miniCalendar = calendar(container);

    expect(miniCalendar).toHaveTextContent("December 2026");
    expect(screen.getByText("1")).toHaveClass("font-bold");
  });

  it("marks every UTC calendar date in a leave range without local date stepping", async () => {
    vi.setSystemTime(new Date("2026-10-01T12:00:00Z"));
    const setDate = vi.spyOn(Date.prototype, "setDate");
    const { container } = await renderPage([
      {
        ...base,
        start_date: "2026-10-24",
        end_date: "2026-10-27",
        status: "approved",
      },
    ]);
    const miniCalendar = calendar(container);

    for (const day of ["24", "25", "26", "27"]) {
      expect(screen.getByText(day)).toHaveClass("font-semibold");
    }
    expect(miniCalendar).toHaveTextContent("October 2026");
    expect(setDate).not.toHaveBeenCalled();
  });

  it("formats date-only request labels in UTC", async () => {
    vi.setSystemTime(new Date("2026-11-01T12:00:00Z"));
    const realToLocaleDateString = Date.prototype.toLocaleDateString;
    vi.spyOn(Date.prototype, "toLocaleDateString").mockImplementation(function (
      this: Date,
      locales,
      options,
    ) {
      return realToLocaleDateString.call(this, locales, {
        timeZone: "America/Los_Angeles",
        ...options,
      });
    });

    await renderPage([
      {
        ...base,
        start_date: "2026-11-16",
        end_date: "2026-11-16",
        status: "approved",
      },
    ]);

    expect(screen.getByText("16 Nov")).toBeInTheDocument();
    expect(screen.queryByText("15 Nov")).toBeNull();
  });

  it("formats both ends of a date-only range in UTC", async () => {
    vi.setSystemTime(new Date("2026-11-01T12:00:00Z"));
    westOfUtcLabels();

    await renderPage([
      {
        ...base,
        start_date: "2026-11-16",
        end_date: "2026-11-18",
        status: "approved",
      },
    ]);

    expect(screen.getByText("16 Nov → 18 Nov 2026")).toBeInTheDocument();
  });

  it("formats the team history dates in UTC for an HR admin", async () => {
    vi.setSystemTime(new Date("2026-11-01T12:00:00Z"));
    getCapabilities.mockResolvedValue({ ...member, hr_admin: true });
    westOfUtcLabels();

    await renderPage(
      [
        {
          ...base,
          is_mine: false,
          start_date: "2026-11-16",
          end_date: "2026-11-16",
          status: "approved",
        },
      ],
      "Recent History",
    );

    expect(screen.getByText("16 Nov")).toBeInTheDocument();
    expect(screen.queryByText("15 Nov")).toBeNull();
  });

  it("keeps approved and pending day colours after month navigation", async () => {
    vi.setSystemTime(new Date("2026-11-01T12:00:00Z"));
    const { container } = await renderPage([
      {
        ...base,
        id: 1,
        start_date: "2026-11-16",
        end_date: "2026-11-16",
        status: "approved",
      },
      {
        ...base,
        id: 2,
        start_date: "2026-11-17",
        end_date: "2026-11-17",
        status: "pending",
      },
    ]);
    const miniCalendar = calendar(container);
    const [previous, next] = miniCalendar.querySelectorAll("button");

    expect(screen.getByText("16")).toHaveClass("font-semibold");
    expect(screen.getByText("17")).toHaveClass("font-semibold");
    fireEvent.click(next);
    expect(miniCalendar).toHaveTextContent("December 2026");
    fireEvent.click(previous);
    expect(miniCalendar).toHaveTextContent("November 2026");
    expect(screen.getByText("16")).toHaveClass("font-semibold");
    expect(screen.getByText("17")).toHaveClass("font-semibold");
  });
});
