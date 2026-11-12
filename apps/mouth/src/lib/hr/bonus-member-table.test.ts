import { describe, expect, it } from "vitest";
import type { Bonus, HREmployee } from "@/types/hr";
import {
  aggregateByMember,
  buildMemberTable,
  groupBonusesByMonth,
} from "./bonus-aggregation";

function bonus(over: Partial<Bonus> & { id: number }): Bonus {
  return {
    practice_id: 1,
    employee_id: 1,
    payroll_period_id: null,
    bonus_rate_id: null,
    practice_type_code: "visa_fake",
    amount_idr: 100,
    status: "approved",
    awarded_at: "2026-07-10T02:00:00.000Z",
    awarded_by: null,
    approved_by: null,
    approved_at: null,
    employee_name: "Fake Setup",
    employee_email: "fake.setup@example.com",
    employee_department: "setup",
    practice_status: "completed",
    client_name: null,
    notes: null,
    source: "practice",
    ...over,
  };
}

function employee(id: number, name: string, department: string): HREmployee {
  return {
    id,
    full_name: name,
    email: `${name.toLowerCase().replace(/ /g, ".")}@example.com`,
    department,
  } as HREmployee;
}

describe("rejected and reversed bonuses are not earned", () => {
  const rows = [
    bonus({ id: 1, amount_idr: 100 }),
    bonus({ id: 2, amount_idr: 40, status: "pending" }),
    bonus({ id: 3, amount_idr: 900, status: "rejected" }),
    bonus({ id: 4, amount_idr: 700, status: "reversed" }),
  ];

  it("keeps them out of member, month and per-member totals but still lists them", () => {
    const [member] = aggregateByMember(rows);
    expect(member.total).toBe(140);
    expect(member.count).toBe(4);
    expect(member.byStatus.rejected).toBe(900);
    const [month] = groupBonusesByMonth(rows);
    expect(month.total).toBe(140);
    expect(month.members[0].bonuses).toHaveLength(4);
  });
});

describe("buildMemberTable", () => {
  it("gives every active employee a row, tax members with nothing as zeros", () => {
    const totals = aggregateByMember([
      bonus({ id: 1, amount_idr: 300 }),
      bonus({
        id: 2,
        employee_id: 2,
        employee_name: "Fake Two",
        employee_email: "fake.two@example.com",
        amount_idr: 100,
      }),
    ]);
    const rows = buildMemberTable(totals, [
      employee(1, "Fake Setup", "setup"),
      employee(2, "Fake Two", "setup"),
      employee(3, "Fake Tax One", "tax"),
      employee(4, "Fake Tax Two", "tax"),
    ]);
    expect(rows.map((r) => r.employeeName)).toEqual([
      "Fake Setup",
      "Fake Two",
      "Fake Tax One",
      "Fake Tax Two",
    ]);
    const taxRow = rows.find((r) => r.employeeName === "Fake Tax One")!;
    expect(taxRow).toMatchObject({
      total: 0,
      count: 0,
      share: 0,
      department: "tax",
    });
    expect(rows[0].share).toBeCloseTo(0.75);
    expect(rows[1].share).toBeCloseTo(0.25);
  });

  it("keeps a member who has bonuses but is no longer in the employee list", () => {
    const totals = aggregateByMember([bonus({ id: 1, employee_id: 9 })]);
    const rows = buildMemberTable(totals, [employee(3, "Fake Tax One", "tax")]);
    expect(rows).toHaveLength(2);
    expect(rows[0].employeeId).toBe(9);
  });

  it("fills a missing department from the employee list", () => {
    const totals = aggregateByMember([
      bonus({ id: 1, employee_department: undefined }),
    ]);
    const [row] = buildMemberTable(totals, [
      employee(1, "Fake Setup", "setup"),
    ]);
    expect(row.department).toBe("setup");
  });
});
