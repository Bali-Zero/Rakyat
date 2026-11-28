import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import BonusesPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listBonuses: vi.fn(),
  listBonusHistorical: vi.fn(),
  listBonusRuns: vi.fn(),
  listEmployees: vi.fn(),
  listBonusPracticeTypes: vi.fn(),
  getCapabilities: vi.fn(),
}));
vi.mock("@/lib/api/hr/hr", () => hrApiMock);
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const bonus = (id: number, awarded_at: string) => ({
  id,
  practice_id: 0,
  employee_id: 1,
  payroll_period_id: null,
  bonus_run_id: null,
  bonus_rate_id: null,
  practice_type_code: "visa_b211",
  amount_idr: 100_000,
  status: "approved",
  awarded_at,
  awarded_by: null,
  approved_by: null,
  approved_at: null,
  employee_name: "Fake Member",
  employee_email: "fake.member@example.test",
  practice_status: "completed",
  client_name: null,
  notes: null,
  source: "practice",
});

const run = (month: string, over: Record<string, unknown>) => ({
  bonus_month: month,
  status: "open",
  ended: true,
  can_approve: true,
  approved_at: null,
  approved_by: null,
  paid_at: null,
  approved_total_idr: 100_000,
  approved_count: 1,
  pending_count: 0,
  pending_total_idr: 0,
  held_count: 0,
  held_total_idr: 0,
  expected_payday: null,
  payday_salary_month: "2026-12-01",
  ...over,
});

const sectionOf = (name: RegExp) =>
  screen.getByRole("button", { name }).closest("section") as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  hrApiMock.listBonuses.mockResolvedValue({
    bonuses: [
      bonus(1, "2026-10-10T02:00:00.000Z"),
      bonus(2, "2026-11-10T02:00:00.000Z"),
      bonus(3, "2026-12-10T02:00:00.000Z"),
      bonus(4, "2027-01-10T02:00:00.000Z"),
    ],
    count: 4,
  });
  hrApiMock.listBonusHistorical.mockResolvedValue({ records: [], count: 0 });
  hrApiMock.listEmployees.mockResolvedValue({ employees: [], count: 0 });
  hrApiMock.listBonusPracticeTypes.mockResolvedValue({ practice_types: [] });
  hrApiMock.getCapabilities.mockResolvedValue({
    hr_admin: true,
    hr_edit: true,
  });
});

describe("bonuses page: one chip per bonus run month", () => {
  it("shows Paid, Awaiting payment and Run to approve, and none for October 2026", async () => {
    hrApiMock.listBonusRuns.mockResolvedValue({
      runs: [
        // 00:30 WITA on 26 Dec: the UTC day would read 25 Dec
        run("2026-11-01", { status: "paid", paid_at: "2026-12-25T16:30:00Z" }),
        run("2026-12-01", {
          status: "approved",
          expected_payday: "2027-01-25",
        }),
        run("2027-01-01", {}),
      ],
    });
    render(<BonusesPage />);
    await screen.findByRole("button", { name: /November 2026/ });
    await waitFor(() =>
      expect(
        within(sectionOf(/November 2026/)).getByText("Paid 26 Dec 2026"),
      ).toBeVisible(),
    );
    expect(
      within(sectionOf(/December 2026/)).getByText(
        "Awaiting payment · Expected 25 Jan 2027",
      ),
    ).toBeVisible();
    expect(
      within(sectionOf(/January 2027/)).getByText("Run to approve"),
    ).toBeVisible();
    expect(
      within(sectionOf(/October 2026/)).queryByText(
        /Run to approve|Awaiting payment|Paid \d/,
      ),
    ).toBeNull();
  });

  it("shows no chip for a month that is still running", async () => {
    hrApiMock.listBonusRuns.mockResolvedValue({
      runs: [run("2026-11-01", { ended: false, can_approve: false })],
    });
    render(<BonusesPage />);
    await screen.findByRole("button", { name: /November 2026/ });
    await waitFor(() => expect(hrApiMock.listBonusRuns).toHaveBeenCalled());
    expect(
      within(sectionOf(/November 2026/)).queryByText(
        /Run to approve|Awaiting payment|Paid \d/,
      ),
    ).toBeNull();
  });

  it("still renders the ledger, with no chip, when listBonusRuns fails", async () => {
    hrApiMock.listBonusRuns.mockRejectedValue(new Error("down"));
    render(<BonusesPage />);
    expect(
      await screen.findByRole("button", { name: /November 2026/ }),
    ).toBeVisible();
    await waitFor(() => expect(hrApiMock.listBonusRuns).toHaveBeenCalled());
    expect(
      screen.queryByText(/Run to approve|Awaiting payment|Paid \d/),
    ).toBeNull();
  });
});
