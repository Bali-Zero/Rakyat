/**
 * The payroll page offers a calculation only when it can stand behind it.
 *
 * Three things it used to get wrong: the month came from the browser's clock,
 * not from the day in Bali the server cuts the month on; Calculate was offered
 * for a month already approved or paid, which the server refuses; and a list
 * that could not be loaded was shown as "No payroll periods yet", with the
 * invitation to calculate.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PayrollPage from "./page";

const hrApi = vi.hoisted(() => ({
  listPayrollPeriods: vi.fn(),
  listPayslips: vi.fn(),
  calculatePayroll: vi.fn(),
  approvePayroll: vi.fn(),
  markPayrollPaid: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const who = vi.hoisted(() => ({ admin: true, payrollWrite: true }));
vi.mock("@/lib/api/hr/hr", () => hrApi);
vi.mock("sonner", () => ({ toast }));
vi.mock("@/lib/hr/use-hr-capabilities", () => ({
  useHrCapabilities: () => ({
    capabilities: { hr_admin: who.admin, hr_payroll_write: who.payrollWrite },
    loaded: true,
  }),
}));

const period = (month: number, year: number, status: string, id = month) => ({
  id,
  payroll_month: month,
  payroll_year: year,
  status,
  payslip_count: 1,
  total_net: 1000,
});

/**
 * The clock of the browser: an instant, and the zone the machine is set to.
 *
 * The zone of the test process cannot be changed from inside a test, so the
 * browser's own calendar is stood in for directly: `getMonth` and
 * `getFullYear` answer what a machine in that zone would. The month in Bali
 * is read through Intl with an explicit zone, which these two do not reach.
 */
function theBrowserIsAt(instant: string, zone: string) {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(instant));
  const local = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "numeric",
  }).formatToParts(new Date(instant));
  const part = (type: string) =>
    Number(local.find((p) => p.type === type)?.value);
  vi.spyOn(Date.prototype, "getMonth").mockReturnValue(part("month") - 1);
  vi.spyOn(Date.prototype, "getFullYear").mockReturnValue(part("year"));
}

/** What the browser's own calendar says, the way the page used to ask it. */
const theBrowsersOwnMonth = () => {
  const now = new Date();
  return [now.getMonth() + 1, now.getFullYear()];
};

const calculate = () => screen.findByRole("button", { name: /Calculate/ });

beforeEach(() => {
  vi.clearAllMocks();
  who.admin = true;
  who.payrollWrite = true;
  hrApi.listPayrollPeriods.mockResolvedValue({ periods: [] });
  hrApi.listPayslips.mockResolvedValue({ payslips: [] });
  hrApi.calculatePayroll.mockResolvedValue({ success: true });
  theBrowserIsAt("2026-11-15T04:00:00Z", "Asia/Makassar");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("the browser these tests stand in for", () => {
  it.each([
    ["2026-11-30T18:00:00Z", "Europe/Rome", [11, 2026]],
    ["2026-11-30T12:00:00Z", "Pacific/Auckland", [12, 2026]],
    ["2026-12-31T17:00:00Z", "Europe/Rome", [12, 2026]],
    ["2026-12-31T12:00:00Z", "Pacific/Auckland", [1, 2027]],
  ])(
    "at %s a machine set to %s says a month that is not Bali's",
    (instant, zone, browsers) => {
      theBrowserIsAt(instant, zone);

      expect(theBrowsersOwnMonth()).toEqual(browsers);
    },
  );
});

describe("the month a calculation is for", () => {
  it.each([
    // In Bali it is already the 1st; in Rome it is still the evening before.
    ["2026-11-30T18:00:00Z", "Europe/Rome", 12, 2026],
    // In Bali it is still the 30th; in Auckland the 1st has begun.
    ["2026-11-30T12:00:00Z", "Pacific/Auckland", 11, 2026],
    // The same edge at the turn of the year.
    ["2026-12-31T17:00:00Z", "Europe/Rome", 1, 2027],
    ["2026-12-31T12:00:00Z", "Pacific/Auckland", 12, 2026],
    // Mid-month, and a browser in Bali at either edge: the same month anywhere.
    ["2026-11-15T04:00:00Z", "Europe/Rome", 11, 2026],
    ["2026-11-30T16:30:00Z", "Asia/Makassar", 12, 2026],
    ["2026-11-30T15:30:00Z", "Asia/Makassar", 11, 2026],
  ])(
    "at %s on a machine set to %s it is %i/%i, the month in Bali",
    async (instant, zone, month, year) => {
      theBrowserIsAt(instant, zone);
      render(<PayrollPage />);

      await userEvent.click(await calculate());

      await waitFor(() =>
        expect(hrApi.calculatePayroll).toHaveBeenCalledTimes(1),
      );
      expect(hrApi.calculatePayroll).toHaveBeenCalledWith(month, year);
    },
  );
});

describe("a month the server has locked", () => {
  it.each(["approved", "paid"])(
    "is not offered a calculation once it is %s, and the page says why",
    async (status) => {
      hrApi.listPayrollPeriods.mockResolvedValue({
        periods: [period(11, 2026, status), period(10, 2026, "paid")],
      });
      render(<PayrollPage />);

      const button = await calculate();

      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(
        `11/2026 is already ${status}.`,
      );
      await userEvent.click(button);
      expect(hrApi.calculatePayroll).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["still open", [period(11, 2026, "calculated")]],
    ["a draft", [period(11, 2026, "draft")]],
    ["not there yet", [period(10, 2026, "paid")]],
    ["there only for another year", [period(11, 2025, "paid")]],
  ])("a month that is %s can be calculated", async (_label, periods) => {
    hrApi.listPayrollPeriods.mockResolvedValue({ periods });
    render(<PayrollPage />);

    const button = await calculate();

    expect(button).toBeEnabled();
    expect(screen.queryByText(/is already/)).toBeNull();
    await userEvent.click(button);
    await waitFor(() =>
      expect(hrApi.calculatePayroll).toHaveBeenCalledWith(11, 2026),
    );
  });

  it("the lock is the lock of the month in Bali, not of the browser's month", async () => {
    // Rome, the evening of 30 November: Bali is in December, which is open.
    theBrowserIsAt("2026-11-30T18:00:00Z", "Europe/Rome");
    hrApi.listPayrollPeriods.mockResolvedValue({
      periods: [period(11, 2026, "paid")],
    });
    render(<PayrollPage />);

    expect(await calculate()).toBeEnabled();
  });
});

describe("a list that could not be loaded", () => {
  it("is told as that to whoever runs payroll, with no invitation to calculate", async () => {
    hrApi.listPayrollPeriods.mockRejectedValueOnce(new Error("500"));
    render(<PayrollPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The payroll periods could not be loaded.",
    );
    expect(screen.queryByText(/No payroll periods yet/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Calculate/ })).toBeNull();
    expect(hrApi.listPayslips).not.toHaveBeenCalled();
  });

  it("can be tried again, and then shows the periods", async () => {
    hrApi.listPayrollPeriods
      .mockRejectedValueOnce(new Error("500"))
      .mockResolvedValueOnce({ periods: [period(11, 2026, "calculated")] });
    render(<PayrollPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Try again" }),
    );

    expect(await screen.findByText("11/2026")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(await calculate()).toBeEnabled();
    expect(hrApi.listPayrollPeriods).toHaveBeenCalledTimes(2);
  });

  it("is told as that to a member of the team, not as no payslips", async () => {
    who.admin = false;
    hrApi.listPayslips
      .mockRejectedValueOnce(new Error("500"))
      .mockResolvedValueOnce({ payslips: [] });
    render(<PayrollPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your payslips could not be loaded.",
    );
    expect(screen.queryByText(/No payslips available yet/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText("No payslips available yet."),
    ).toBeInTheDocument();
    expect(hrApi.listPayrollPeriods).not.toHaveBeenCalled();
  });
});

describe("a list that loaded empty", () => {
  it("still says there are no periods and how to start", async () => {
    render(<PayrollPage />);

    expect(
      await screen.findByText(/No payroll periods yet\./),
    ).toHaveTextContent('Click "Calculate Current Month" to start.');
    expect(screen.queryByRole("alert")).toBeNull();
    expect(await calculate()).toBeEnabled();
  });
});

describe("after a calculation", () => {
  it("that the server refuses, its message is shown and the list stays", async () => {
    hrApi.listPayrollPeriods.mockResolvedValue({
      periods: [period(11, 2026, "calculated")],
    });
    hrApi.calculatePayroll.mockRejectedValue(new Error("Not this month"));
    render(<PayrollPage />);

    await userEvent.click(await calculate());

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Not this month"),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByText("11/2026")).toBeInTheDocument();
    expect(hrApi.listPayrollPeriods).toHaveBeenCalledTimes(1);
    expect(await calculate()).toBeEnabled();
  });

  it("that succeeded, a list that cannot be read again is not a calculation that failed", async () => {
    hrApi.listPayrollPeriods
      .mockResolvedValueOnce({ periods: [] })
      .mockRejectedValueOnce(new Error("500"));
    render(<PayrollPage />);

    await userEvent.click(await calculate());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The payroll periods could not be loaded.",
    );
    expect(toast.success).toHaveBeenCalledWith("Payroll calculated");
    expect(toast.error).not.toHaveBeenCalled();
    expect(hrApi.calculatePayroll).toHaveBeenCalledTimes(1);
  });

  it("that succeeded, the new list is shown", async () => {
    hrApi.listPayrollPeriods
      .mockResolvedValueOnce({ periods: [] })
      .mockResolvedValueOnce({ periods: [period(11, 2026, "calculated")] });
    render(<PayrollPage />);

    await userEvent.click(await calculate());

    expect(await screen.findByText("11/2026")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Payroll calculated");
  });
});
