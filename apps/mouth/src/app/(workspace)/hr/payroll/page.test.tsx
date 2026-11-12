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
vi.mock("@/lib/hr/use-hr-capabilities", () => ({
  useHrCapabilities: () => ({
    capabilities: { hr_admin: true },
    loaded: true,
  }),
}));

describe("payroll approve", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
});
