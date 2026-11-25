import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { dueMonthLabel, isDueSoon, ObligationsTable } from "./ObligationsTable";
import type { CatalogRuleOut, ObligationOut } from "./types";

const rule: CatalogRuleOut = {
  id: "synthetic_rule",
  name: "Synthetic rule",
  authority: "Test",
  legal_source: "Test source",
  verified: true,
  frequency: "monthly",
  roll: "none",
  needs_review_reason: null,
  trigger: null,
  notes: null,
};
const row = (status: ObligationOut["status"] = "proposed"): ObligationOut => ({
  id: 101,
  client_id: 9,
  rule_id: rule.id,
  period_key: "2026-01",
  due_date: "2026-01-11",
  status,
  needs_review_reason: null,
  reviewer_email: null,
  reviewed_at: null,
  review_note: null,
  alert_id: null,
  created_at: null,
  updated_at: null,
});
function renderTable(items = [row()], catalog = new Map([[rule.id, rule]])) {
  return render(
    <ObligationsTable
      items={items}
      catalog={catalog}
      clientNames={{}}
      groupByMonth={false}
      notes={{}}
      evidenceRefs={{}}
      declarations={{}}
      rowError={{}}
      busyIds={new Set()}
      onNoteChange={vi.fn()}
      onApprove={vi.fn()}
      onReject={vi.fn()}
      onComplete={vi.fn()}
      onEvidenceChange={vi.fn()}
      onDeclarationChange={vi.fn()}
    />,
  );
}

afterEach(() => vi.useRealTimers());

describe("ObligationsTable", () => {
  it("renders verified and unverified badges only for catalog rules", () => {
    const { rerender } = renderTable([row()], new Map([[rule.id, rule]]));
    expect(screen.getByText("verified")).toBeVisible();
    rerender(
      <ObligationsTable
        items={[row()]}
        catalog={new Map([[rule.id, { ...rule, verified: false }]])}
        clientNames={{}}
        groupByMonth={false}
        notes={{}}
        evidenceRefs={{}}
        declarations={{}}
        rowError={{}}
        busyIds={new Set()}
        onNoteChange={vi.fn()}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onComplete={vi.fn()}
        onEvidenceChange={vi.fn()}
        onDeclarationChange={vi.fn()}
      />,
    );
    expect(screen.getByText("unverified")).toBeVisible();
    rerender(
      <ObligationsTable
        items={[row()]}
        catalog={new Map()}
        clientNames={{}}
        groupByMonth={false}
        notes={{}}
        evidenceRefs={{}}
        declarations={{}}
        rowError={{}}
        busyIds={new Set()}
        onNoteChange={vi.fn()}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onComplete={vi.fn()}
        onEvidenceChange={vi.fn()}
        onDeclarationChange={vi.fn()}
      />,
    );
    expect(screen.queryByText("unverified")).not.toBeInTheDocument();
    expect(screen.queryByText(/·\s*unverified/)).not.toBeInTheDocument();
  });

  it("labels due dates in the Makassar seven-day boundary", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-04T00:00:00+08:00"));
    expect(dueMonthLabel("2026-01-11")).toBe("January 2026");
    expect(dueMonthLabel("not-a-date")).toBe("Unscheduled");
    expect(isDueSoon("2026-01-11")).toBe(true);
    expect(isDueSoon("2026-01-12")).toBe(false);
  });

  it("does not warn for due-soon done or rejected rows", () => {
    const { getByTestId } = renderTable([
      row("proposed"),
      { ...row("done"), id: 102 },
      { ...row("rejected"), id: 103 },
    ]);
    expect(
      within(getByTestId("obligation-row-101")).getByText("2026-01-11"),
    ).toHaveStyle({ color: "var(--state-warning)" });
    expect(
      within(getByTestId("obligation-row-102")).getByText("2026-01-11"),
    ).not.toHaveStyle({ color: "var(--state-warning)" });
    expect(
      within(getByTestId("obligation-row-103")).getByText("2026-01-11"),
    ).not.toHaveStyle({ color: "var(--state-warning)" });
  });
});
