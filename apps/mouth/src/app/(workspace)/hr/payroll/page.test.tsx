import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PayrollPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listPayrollPeriods: vi.fn(),
  listPayslips: vi.fn(),
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
