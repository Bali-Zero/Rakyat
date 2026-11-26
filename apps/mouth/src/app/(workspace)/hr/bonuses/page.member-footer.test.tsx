import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import BonusesPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listBonuses: vi.fn(),
  listBonusHistorical: vi.fn(),
  listEmployees: vi.fn(),
  listBonusPracticeTypes: vi.fn(),
  getCapabilities: vi.fn(),
}));

vi.mock("@/lib/api/hr/hr", () => hrApiMock);
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function bonus(over: Record<string, unknown> & { id: number }) {
  return {
    practice_id: 0,
    employee_id: 1,
    payroll_period_id: null,
    bonus_rate_id: null,
    practice_type_code: "synthetic_bonus",
    amount_idr: 100_000,
    status: "pending",
    awarded_at: "2026-05-10T02:00:00.000Z",
    awarded_by: null,
    approved_by: null,
    approved_at: null,
    employee_name: "Payroll Person",
    employee_email: "payroll@example.test",
    practice_status: "completed",
    client_name: null,
    notes: null,
    source: "practice",
    ...over,
  };
}

function setup(bonuses: ReturnType<typeof bonus>[], offPayroll = false) {
  hrApiMock.listBonuses.mockResolvedValue({ bonuses, count: bonuses.length });
  hrApiMock.listBonusHistorical.mockResolvedValue({ records: [], count: 0 });
  hrApiMock.listEmployees.mockResolvedValue({
    employees: offPayroll ? [{ id: 2, on_payroll: false }] : [],
    count: offPayroll ? 1 : 0,
  });
  hrApiMock.listBonusPracticeTypes.mockResolvedValue({
    practice_types: [],
    count: 0,
  });
  hrApiMock.getCapabilities.mockResolvedValue({
    hr_admin: false,
    accounting_admin: false,
    has_employee_record: true,
    people_scope: null,
    hr_edit: false,
  });
}

beforeEach(() => vi.clearAllMocks());

function idr(text: string | null): number {
  const digits = (text ?? "").replace(/[^0-9]/g, "");
  return digits ? Number(digits) : 0;
}

function amount(cell: HTMLElement): number {
  const money = cell.cloneNode(true) as HTMLElement;
  money
    .querySelectorAll("div.flex-col > span.rounded-full")
    .forEach((share) => share.remove());
  return idr(money.textContent);
}

async function memberTable() {
  const table = await screen.findByRole("table");
  const [body] = within(table).getAllByRole("rowgroup").slice(1, 2);
  const bodyRows = within(body).getAllByRole("row");
  const rows = within(table).getAllByRole("row");
  const footer = within(rows[rows.length - 1]).getAllByRole("cell");
  const columnSum = (column: number) =>
    bodyRows.reduce(
      (sum, row) => sum + amount(within(row).getAllByRole("cell")[column]),
      0,
    );
  return { bodyRows, footer, columnSum };
}

function kpiTotal(): number {
  const label = screen
    .getAllByText("Total")
    .find((element) => element.tagName === "DIV");
  return idr(label?.parentElement?.textContent ?? null);
}

const offPayroll = {
  employee_id: 2,
  employee_name: "Off Payroll Person",
  employee_email: "off-payroll@example.test",
};

describe("BonusesPage member footer", () => {
  it("adds up only the payroll rows above it while the KPI and month list keep every ledger row", async () => {
    setup(
      [
        bonus({ id: 1, amount_idr: 100_000, status: "pending" }),
        bonus({ id: 2, amount_idr: 50_000, status: "approved" }),
        bonus({ id: 3, ...offPayroll, amount_idr: 200_000, status: "pending" }),
        bonus({
          id: 4,
          ...offPayroll,
          amount_idr: 300_000,
          status: "approved",
        }),
        bonus({
          id: 5,
          ...offPayroll,
          amount_idr: 400_000,
          status: "approved",
          awarded_at: "2026-06-10T02:00:00.000Z",
        }),
      ],
      true,
    );

    render(<BonusesPage />);

    const { bodyRows, footer, columnSum } = await memberTable();
    expect(bodyRows).toHaveLength(1);
    expect(bodyRows[0]).toHaveTextContent("Payroll Person");
    expect(footer[1].textContent).toBe("1");
    expect(footer[2].textContent).toBe("2");
    expect(idr(footer[3].textContent)).toBe(100_000);
    expect(idr(footer[4].textContent)).toBe(50_000);
    expect(idr(footer[5].textContent)).toBe(150_000);
    for (const column of [3, 4, 5]) {
      expect(idr(footer[column].textContent)).toBe(columnSum(column));
    }
    expect(kpiTotal()).toBe(1_050_000);
    expect(
      (await screen.findAllByText("Off Payroll Person")).length,
    ).toBeGreaterThan(0);
  });

  it("keeps footer and KPI as today when every ledger person is on payroll", async () => {
    setup([
      bonus({ id: 1, amount_idr: 100_000, status: "pending" }),
      bonus({
        id: 2,
        amount_idr: 70_000,
        status: "approved",
        awarded_at: "2026-06-10T02:00:00.000Z",
      }),
    ]);

    render(<BonusesPage />);

    const { footer, columnSum } = await memberTable();
    expect(footer[1].textContent).toBe("2");
    expect(footer[2].textContent).toBe("2");
    expect(idr(footer[3].textContent)).toBe(100_000);
    expect(idr(footer[4].textContent)).toBe(70_000);
    expect(idr(footer[5].textContent)).toBe(170_000);
    for (const column of [3, 4, 5]) {
      expect(idr(footer[column].textContent)).toBe(columnSum(column));
    }
    expect(kpiTotal()).toBe(170_000);
  });
});
