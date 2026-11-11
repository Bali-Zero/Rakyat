import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { caps, getDashboard, getMyDashboard } = vi.hoisted(() => ({
  caps: vi.fn(),
  getDashboard: vi.fn(),
  getMyDashboard: vi.fn(),
}));

vi.mock("@/lib/api/hr/hr", () => ({
  getCapabilities: caps,
  getDashboard,
  getMyDashboard,
}));

import HRDashboardPage from "../page";

const ADMIN = {
  employee_count: 8,
  pending_bonuses: { count: 0, total: 0 },
  pending_leave_requests: 0,
  current_period: null,
  month: 11,
  year: 2026,
};
const PERSONAL = {
  month_bonuses: { count: 0, total: 0 },
  latest_payslip: null,
  leave_balances: [],
};

describe("HR dashboard", () => {
  beforeEach(() => {
    caps.mockReset();
    getDashboard.mockReset();
    getMyDashboard.mockReset();
  });

  it("shows a member their own card and never calls the admin route", async () => {
    caps.mockResolvedValue({
      hr_admin: false,
      accounting_admin: false,
      has_employee_record: true,
    });
    getMyDashboard.mockResolvedValue(PERSONAL);
    render(<HRDashboardPage />);
    await waitFor(() => expect(getMyDashboard).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByText("Active Employees")).not.toBeInTheDocument(),
    );
    expect(getDashboard).not.toHaveBeenCalled();
  });

  it("keeps the team overview when the admin's own card fails", async () => {
    caps.mockResolvedValue({
      hr_admin: true,
      accounting_admin: true,
      has_employee_record: true,
    });
    getDashboard.mockResolvedValue(ADMIN);
    getMyDashboard.mockRejectedValue(new Error("boom"));
    render(<HRDashboardPage />);
    await waitFor(() =>
      expect(screen.getByText("Active Employees")).toBeInTheDocument(),
    );
  });

  it("says access could not be loaded, not that the record is missing", async () => {
    caps.mockRejectedValue(new Error("503"));
    render(<HRDashboardPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/Could not load your HR access/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/No HR record/)).not.toBeInTheDocument();
    expect(getMyDashboard).not.toHaveBeenCalled();
  });
});
