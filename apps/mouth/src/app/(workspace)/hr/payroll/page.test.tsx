import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PayrollPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listPayrollPeriods: vi.fn(),
  listPayslips: vi.fn(),
  listBonusRuns: vi.fn(),
  calculatePayroll: vi.fn(),
  approvePayroll: vi.fn(),
  markPayrollPaid: vi.fn(),
}));
const toastMock = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("@/lib/api/hr/hr", () => hrApiMock);
vi.mock("sonner", () => ({ toast: toastMock }));
const capsState = vi.hoisted(() => ({ payrollWrite: true }));
vi.mock("@/lib/hr/use-hr-capabilities", () => ({
  useHrCapabilities: () => ({
    capabilities: { hr_admin: true, hr_payroll_write: capsState.payrollWrite },
    loaded: true,
  }),
}));

describe("payroll approve", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capsState.payrollWrite = true;
    hrApiMock.listBonusRuns.mockResolvedValue({ runs: [] });
    hrApiMock.listPayrollPeriods.mockResolvedValue({
      periods: [
        {
          id: 4,
          payroll_month: 11,
          payroll_year: 2026,
          status: "calculated",
          payslip_count: 1,
          total_net: 1000,
        },
      ],
    });
  });

  it("shows the server's roster-changed message the way it shows the bonus-drift one", async () => {
    const msg =
      "The team changed since this payroll was calculated: calculate again";
    hrApiMock.approvePayroll.mockRejectedValue(new Error(msg));
    render(<PayrollPage />);
    await userEvent.click(
      await screen.findByRole("button", { name: /Approve/ }),
    );
    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith(msg));
  });

  it("hides Calculate, Approve and Mark Paid from an admin who is not an HR writer", async () => {
    capsState.payrollWrite = false;
    hrApiMock.listPayrollPeriods.mockResolvedValue({
      periods: [
        {
          id: 4,
          payroll_month: 11,
          payroll_year: 2026,
          status: "calculated",
          payslip_count: 1,
          total_net: 1000,
        },
        {
          id: 5,
          payroll_month: 10,
          payroll_year: 2026,
          status: "approved",
          payslip_count: 1,
          total_net: 1000,
        },
      ],
    });
    render(<PayrollPage />);
    expect(await screen.findByText("11/2026")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Calculate/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Mark Paid/ })).toBeNull();
  });

  it("shows the three actions to an HR writer", async () => {
    hrApiMock.listPayrollPeriods.mockResolvedValue({
      periods: [
        {
          id: 4,
          payroll_month: 11,
          payroll_year: 2026,
          status: "calculated",
          payslip_count: 1,
          total_net: 1,
        },
        {
          id: 5,
          payroll_month: 10,
          payroll_year: 2026,
          status: "approved",
          payslip_count: 1,
          total_net: 1,
        },
      ],
    });
    render(<PayrollPage />);
    expect(
      await screen.findByRole("button", { name: /Calculate/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Mark Paid/ }),
    ).toBeInTheDocument();
  });
});

describe("payroll page bonus runs", () => {
  const bonusRun = {
    bonus_month: "2026-11-01",
    status: "open",
    ended: false,
    can_approve: false,
    approved_at: null,
    approved_by: null,
    paid_at: null,
    approved_total_idr: 0,
    approved_count: 0,
    pending_count: 0,
    pending_total_idr: 0,
    held_count: 0,
    held_total_idr: 0,
    expected_payday: null,
    payday_salary_month: "2026-12-01",
  };

  beforeEach(() => {
    vi.clearAllMocks();
    capsState.payrollWrite = true;
    hrApiMock.listPayrollPeriods.mockResolvedValue({ periods: [] });
  });

  it("lists the bonus months below the periods, with no payday and a disabled Approve before 1 Dec", async () => {
    hrApiMock.listBonusRuns.mockResolvedValue({ runs: [bonusRun] });
    render(<PayrollPage />);
    expect(await screen.findByText("November 2026")).toBeVisible();
    expect(screen.getByText("Payday known once the month ends")).toBeVisible();
    expect(screen.getByRole("button", { name: /Approve run/ })).toBeDisabled();
  });

  it("says the runs could not be loaded and still shows the periods when listBonusRuns fails", async () => {
    hrApiMock.listBonusRuns.mockRejectedValue(new Error("down"));
    render(<PayrollPage />);
    expect(
      await screen.findByText("The bonus runs could not be loaded."),
    ).toBeVisible();
    expect(
      screen.getByText("No payroll periods yet.", { exact: false }),
    ).toBeVisible();
  });
});
