/**
 * The bonuses page reads two things: the ledger, and a recap of the months paid from
 * a PDF list before the ledger existed. A PDF month the ledger does not hold is shown
 * as "PDF recap only", with the PDF total as the amount to pay.
 *
 * When the LEDGER request fails, every PDF month looked exactly like that. The page
 * must say the ledger did not load, and give no figure to pay from. Invented values only.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import BonusesPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listBonuses: vi.fn(),
  listBonusHistorical: vi.fn(),
  approveBonus: vi.fn(),
  rejectBonus: vi.fn(),
  deleteBonus: vi.fn(),
  createManualBonus: vi.fn(),
  listEmployees: vi.fn(),
  listBonusPracticeTypes: vi.fn(),
  getCapabilities: vi.fn(),
}));

vi.mock("@/lib/api/hr/hr", () => hrApiMock);

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const PDF_MONTH = {
  id: 1,
  employee_name: "PERSON ONE",
  employee_id: 900099,
  bonus_month: 5,
  bonus_year: 2026,
  total_amount_idr: 2_000_000,
  task_count: 6,
  source_pdf: "LIST BONUS MAY 2026.pdf",
  accounting_total_data: null,
  accounting_not_paid: null,
  accounting_paid: null,
  imported_at: "2026-06-01T00:00:00.000Z",
  notes: null,
};

const LEDGER_ROW = {
  id: 1,
  practice_id: 0,
  employee_id: 900003,
  payroll_period_id: null,
  bonus_rate_id: null,
  practice_type_code: "visa_b211",
  amount_idr: 750_000,
  status: "approved",
  awarded_at: "2026-07-10T02:00:00.000Z",
  awarded_by: null,
  approved_by: null,
  approved_at: null,
  employee_name: "Person Two",
  employee_email: "person.two@example.test",
  practice_status: "completed",
  client_name: null,
  notes: null,
  source: "practice",
};

beforeEach(() => {
  vi.clearAllMocks();
  hrApiMock.listBonuses.mockRejectedValue(new Error("503"));
  hrApiMock.listBonusHistorical.mockResolvedValue({
    records: [PDF_MONTH],
    count: 1,
  });
  hrApiMock.listEmployees.mockResolvedValue({ employees: [], count: 0 });
  hrApiMock.listBonusPracticeTypes.mockResolvedValue({
    practice_types: [],
    count: 0,
  });
  hrApiMock.getCapabilities.mockResolvedValue({
    hr_admin: true,
    accounting_admin: true,
    has_employee_record: true,
    people_scope: null,
    hr_edit: true,
  });
});

describe("BonusesPage when the ledger does not load", () => {
  it("says so, and names no PDF month as missing from the ledger", async () => {
    render(<BonusesPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The bonus ledger could not be loaded");
    expect(screen.queryByText(/PDF recap only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/use the PDF total/)).not.toBeInTheDocument();
    expect(screen.queryByText(/May 2026/)).not.toBeInTheDocument();
  });

  it("does not call a failed load an empty ledger", async () => {
    hrApiMock.listBonusHistorical.mockResolvedValue({ records: [], count: 0 });
    render(<BonusesPage />);

    await screen.findByRole("alert");
    expect(
      screen.queryByText(/No bonuses for this selection/),
    ).not.toBeInTheDocument();
  });

  it("loads the ledger again on Retry and then reads the PDF month as before", async () => {
    const user = userEvent.setup();
    render(<BonusesPage />);
    await screen.findByRole("alert");
    hrApiMock.listBonuses.mockResolvedValue({
      bonuses: [LEDGER_ROW],
      count: 1,
    });

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("July 2026")).toBeInTheDocument();
    // The ledger is there now: May really is a month it does not hold.
    expect(screen.getByRole("button", { name: /May 2026/ })).toHaveTextContent(
      "PDF recap only — not yet in the ledger",
    );
  });

  it.each([
    ["an empty object", {}],
    ["bonuses null", { bonuses: null, count: 0 }],
    ["bonuses a string", { bonuses: "none", count: 0 }],
    ["bonuses an object", { bonuses: { 0: LEDGER_ROW }, count: 1 }],
    ["no body at all", undefined],
  ])(
    "takes an answer without a list of bonuses (%s) as a failed load",
    async (_label, body) => {
      hrApiMock.listBonuses.mockResolvedValue(body);
      render(<BonusesPage />);

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("The bonus ledger could not be loaded");
      expect(screen.queryByText(/PDF recap only/)).not.toBeInTheDocument();
      expect(screen.queryByText(/use the PDF total/)).not.toBeInTheDocument();
      expect(
        screen.queryByText(/No bonuses for this selection/),
      ).not.toBeInTheDocument();
    },
  );

  it("shows nothing of the ledger while it is not loaded: no total, no table, no export", async () => {
    render(<BonusesPage />);

    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /CSV/ })).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("Total")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add bonus" })).toBeNull();
  });

  it("drops the ledger it had when a later reload fails", async () => {
    // A first load that works, then the reload after "Add bonus" fails.
    hrApiMock.listBonuses.mockResolvedValue({
      bonuses: [LEDGER_ROW],
      count: 1,
    });
    hrApiMock.listEmployees.mockResolvedValue({
      employees: [
        {
          id: 900003,
          full_name: "Person Two",
          email: "person.two@example.test",
          department: "tax",
        },
      ],
      count: 1,
    });
    hrApiMock.listBonusPracticeTypes.mockResolvedValue({
      practice_types: [
        {
          code: "tax_fake_report",
          name: "Fake annual report",
          category: "tax",
          group: "tax",
        },
      ],
      count: 1,
    });
    hrApiMock.createManualBonus.mockResolvedValue({
      success: true,
      bonus: { id: 99 },
    });
    const user = userEvent.setup();
    render(<BonusesPage />);
    expect(await screen.findByText("July 2026")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /CSV/ })).toBeEnabled();

    await user.click(await screen.findByRole("button", { name: "Add bonus" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Person"), "900003");
    await user.click(within(dialog).getByRole("combobox", { name: "Service" }));
    await user.click(
      await screen.findByRole("option", { name: "Fake annual report" }),
    );
    await user.type(within(dialog).getByLabelText("Amount (IDR)"), "250000");
    await user.type(within(dialog).getByLabelText(/Reason/), "Invented reason");
    hrApiMock.listBonuses.mockRejectedValue(new Error("503"));
    await user.click(within(dialog).getByRole("button", { name: "Add bonus" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The bonus ledger could not be loaded");
    expect(screen.queryByText("July 2026")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: /CSV/ })).toBeNull();

    // And it comes back whole on Retry.
    hrApiMock.listBonuses.mockResolvedValue({
      bonuses: [LEDGER_ROW],
      count: 1,
    });
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("July 2026")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows no alert when the ledger loads", async () => {
    hrApiMock.listBonuses.mockResolvedValue({
      bonuses: [LEDGER_ROW],
      count: 1,
    });
    render(<BonusesPage />);

    await screen.findByText("July 2026");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
