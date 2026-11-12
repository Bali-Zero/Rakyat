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
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// Synthetic people only.
const EMPLOYEES = [
  {
    id: 1,
    full_name: "Fake Setup",
    email: "fake.setup@example.com",
    department: "setup",
  },
  {
    id: 2,
    full_name: "Fake Tax One",
    email: "fake.tax1@example.com",
    department: "tax",
  },
  {
    id: 3,
    full_name: "Fake Tax Two",
    email: "fake.tax2@example.com",
    department: "tax",
  },
  {
    id: 4,
    full_name: "Fake Office",
    email: "fake.office@example.com",
    department: "office",
  },
];

const TYPES = [
  { code: "visa_fake", name: "Fake visa", category: "visa", group: "visa" },
  {
    code: "tax_fake_report",
    name: "Fake annual report",
    category: "tax",
    group: "tax",
  },
];

function bonus(over: Record<string, unknown> & { id: number }) {
  return {
    practice_id: 10,
    employee_id: 1,
    payroll_period_id: null,
    bonus_rate_id: null,
    practice_type_code: "visa_fake",
    amount_idr: 100_000,
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

const LEDGER = [
  bonus({ id: 1, amount_idr: 600_000, status: "pending" }),
  bonus({
    id: 2,
    practice_id: null,
    source: "manual",
    reason: "Weekend filing",
    created_by: "fake.admin@example.com",
    practice_type_code: "manual",
    employee_id: 4,
    employee_name: "Fake Office",
    employee_email: "fake.office@example.com",
    employee_department: "office",
    amount_idr: 200_000,
    status: "pending",
  }),
  bonus({
    id: 3,
    practice_id: null,
    source: "manual",
    reason: "Already approved extra",
    practice_type_code: "tax_fake_report",
    employee_id: 2,
    employee_name: "Fake Tax One",
    employee_email: "fake.tax1@example.com",
    employee_department: "tax",
    amount_idr: 200_000,
    status: "approved",
  }),
];

function asAdmin(isAdmin: boolean) {
  hrApiMock.getCapabilities.mockResolvedValue({
    hr_admin: isAdmin,
    accounting_admin: false,
    has_employee_record: true,
    people_scope: isAdmin ? "all" : null,
    hr_edit: isAdmin,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  hrApiMock.listBonuses.mockResolvedValue({
    bonuses: LEDGER,
    count: LEDGER.length,
  });
  hrApiMock.listBonusHistorical.mockResolvedValue({ records: [], count: 0 });
  hrApiMock.listEmployees.mockResolvedValue({ employees: EMPLOYEES, count: 4 });
  hrApiMock.listBonusPracticeTypes.mockResolvedValue({
    practice_types: TYPES,
    count: 2,
  });
  asAdmin(true);
});

async function openPeople(user: ReturnType<typeof userEvent.setup>) {
  for (const name of [
    /Fake Setup.*1 bonus/,
    /Fake Office.*1 bonus/,
    /Fake Tax One.*1 bonus/,
  ]) {
    await user.click(await screen.findByRole("button", { name }));
  }
}

describe("per-member table", () => {
  it("lists every active member, tax members with nothing as soft dashes, with department chips", async () => {
    render(<BonusesPage />);
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    const text = (name: string) =>
      rows.find((r) => r.textContent?.startsWith(name));

    const taxTwo = text("Fake Tax Two")!;
    expect(taxTwo).toHaveTextContent("Tax");
    expect(taxTwo.textContent).not.toMatch(/Rp/);
    expect(taxTwo).toHaveTextContent("—");
    expect(text("Fake Setup")).toHaveTextContent("Setup");
    expect(text("Fake Setup")).toHaveTextContent("Rp 600.000");
    // The share is text in a pill, never a style or an attribute.
    expect(text("Fake Setup")).toHaveTextContent("60%");
    // footer: 600k + 200k + 200k
    expect(rows[rows.length - 1]).toHaveTextContent("Rp 1.000.000");
  });

  it("the department chips narrow rows and the table to that team", async () => {
    const user = userEvent.setup();
    render(<BonusesPage />);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Tax" }));
    await waitFor(() => {
      const rows = within(screen.getByRole("table")).getAllByRole("row");
      expect(rows.some((r) => r.textContent?.startsWith("Fake Setup"))).toBe(
        false,
      );
      expect(rows.some((r) => r.textContent?.startsWith("Fake Tax Two"))).toBe(
        true,
      );
    });
    expect(screen.getByRole("button", { name: "Tax" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("row actions", () => {
  it("an HR admin sees Approve and Reject on pending rows, Delete only on a pending manual one", async () => {
    const user = userEvent.setup();
    render(<BonusesPage />);
    await openPeople(user);

    const practiceRow = screen.getByText("visa fake").closest("li")!;
    expect(within(practiceRow).getByText("Practice")).toBeInTheDocument();
    expect(
      within(practiceRow).getByRole("button", { name: /Approve/ }),
    ).toBeInTheDocument();
    expect(
      within(practiceRow).getByRole("button", { name: /Reject/ }),
    ).toBeInTheDocument();
    expect(
      within(practiceRow).queryByRole("button", { name: /Delete/ }),
    ).toBeNull();

    const manualPending = screen.getByText(/Weekend filing/).closest("li")!;
    expect(within(manualPending).getByText("Manual")).toBeInTheDocument();
    expect(manualPending).toHaveTextContent("added by fake.admin@example.com");
    expect(
      within(manualPending).getByRole("button", { name: /Delete/ }),
    ).toBeInTheDocument();

    const manualApproved = screen
      .getByText(/Already approved extra/)
      .closest("li")!;
    expect(within(manualApproved).queryByRole("button")).toBeNull();
  });

  it("a team member sees the rows and the source, but no action and no Add bonus", async () => {
    asAdmin(false);
    const user = userEvent.setup();
    render(<BonusesPage />);
    await openPeople(user);
    expect(screen.getByText(/Weekend filing/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Approve|Reject|Delete/ }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Add bonus" })).toBeNull();
  });

  it("reject asks first, then rejects and drops the amount from the totals", async () => {
    hrApiMock.rejectBonus.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<BonusesPage />);
    await openPeople(user);
    const row = screen.getByText("visa fake").closest("li")!;
    await user.click(within(row).getByRole("button", { name: /Reject/ }));
    expect(hrApiMock.rejectBonus).not.toHaveBeenCalled();
    await user.click(within(row).getByRole("button", { name: /Yes, reject/ }));
    expect(hrApiMock.rejectBonus).toHaveBeenCalledWith(1);
    await waitFor(() =>
      expect(within(row).getByText("rejected")).toBeInTheDocument(),
    );
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows[rows.length - 1]).toHaveTextContent("Rp 400.000");
  });

  it("delete asks first, then removes the manual row", async () => {
    hrApiMock.deleteBonus.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<BonusesPage />);
    await openPeople(user);
    const row = screen.getByText(/Weekend filing/).closest("li")!;
    await user.click(within(row).getByRole("button", { name: /Delete/ }));
    await user.click(within(row).getByRole("button", { name: /Yes, delete/ }));
    expect(hrApiMock.deleteBonus).toHaveBeenCalledWith(2);
    await waitFor(() =>
      expect(screen.queryByText(/Weekend filing/)).toBeNull(),
    );
  });
});

describe("Add bonus", () => {
  async function openForm(user: ReturnType<typeof userEvent.setup>) {
    render(<BonusesPage />);
    await user.click(await screen.findByRole("button", { name: "Add bonus" }));
    return await screen.findByRole("dialog");
  }

  it("offers every active person grouped by department, the tax team included", async () => {
    const user = userEvent.setup();
    const dialog = await openForm(user);
    const select = within(dialog).getByLabelText("Person");
    const groups = [...select.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groups).toEqual(["Setup", "Tax", "Office"]);
    expect(
      within(select).getByRole("option", { name: "Fake Tax Two" }),
    ).toBeInTheDocument();
  });

  it("explains what is missing and sends nothing", async () => {
    const user = userEvent.setup();
    const dialog = await openForm(user);
    await user.click(within(dialog).getByRole("button", { name: "Add bonus" }));
    expect(
      within(dialog)
        .getAllByRole("alert")
        .map((a) => a.textContent),
    ).toEqual([
      "Choose who gets the bonus.",
      "Enter a whole amount above 0.",
      "Say why, in at least 3 characters.",
    ]);
    expect(hrApiMock.createManualBonus).not.toHaveBeenCalled();
  });

  it("submits a tax bonus with a service, reloads the ledger and closes", async () => {
    hrApiMock.createManualBonus.mockResolvedValue({
      success: true,
      bonus: { id: 99 },
    });
    const user = userEvent.setup();
    const dialog = await openForm(user);
    await user.selectOptions(within(dialog).getByLabelText("Person"), "3");
    await user.click(within(dialog).getByRole("combobox", { name: "Service" }));
    await user.click(
      await screen.findByRole("option", { name: "Fake annual report" }),
    );
    await user.type(within(dialog).getByLabelText("Amount (IDR)"), "250000");
    await user.type(
      within(dialog).getByLabelText(/Reason/),
      "Closing the annual file",
    );
    const calls = hrApiMock.listBonuses.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: "Add bonus" }));
    await waitFor(() =>
      expect(hrApiMock.createManualBonus).toHaveBeenCalledTimes(1),
    );
    expect(hrApiMock.createManualBonus).toHaveBeenCalledWith({
      employee_id: 3,
      amount_idr: 250000,
      reason: "Closing the annual file",
      practice_type_code: "tax_fake_report",
      awarded_on: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(hrApiMock.listBonuses.mock.calls.length).toBe(calls + 1);
  });

  it("keeps the form open when the server refuses (locked month)", async () => {
    hrApiMock.createManualBonus.mockRejectedValue(
      new Error("Payroll for that month is already approved"),
    );
    const user = userEvent.setup();
    const dialog = await openForm(user);
    await user.selectOptions(within(dialog).getByLabelText("Person"), "1");
    await user.type(within(dialog).getByLabelText("Amount (IDR)"), "1000");
    await user.type(within(dialog).getByLabelText(/Reason/), "Late extra");
    await user.click(within(dialog).getByRole("button", { name: "Add bonus" }));
    await waitFor(() => expect(hrApiMock.createManualBonus).toHaveBeenCalled());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("Tax filter", () => {
  it("still lists the tax team at a dash when nothing is left to show for them", async () => {
    // Only non-tax bonuses exist and there is no PDF recap: the Tax chip empties the months.
    const user = userEvent.setup();
    render(<BonusesPage />);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Tax" }));
    await user.click(screen.getByLabelText("Filter by status"));
    await user.selectOptions(
      screen.getByLabelText("Filter by status"),
      "pending",
    );
    await screen.findByText(/No bonuses for this selection/);
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    const two = rows.find((r) => r.textContent?.startsWith("Fake Tax Two"))!;
    const one = rows.find((r) => r.textContent?.startsWith("Fake Tax One"))!;
    for (const row of [one, two]) {
      expect(row).toHaveTextContent("—");
      expect(row.textContent).not.toMatch(/Rp/);
    }
    expect(rows.some((r) => r.textContent?.startsWith("Fake Setup"))).toBe(
      false,
    );
  });
});

describe("Sentry protection", () => {
  // htmlTreeAsString (Sentry) serialises id, class and these attributes, never text.
  const RECORDED = ["aria-label", "title", "name", "alt", "id"];
  const FIXED_UI_STRINGS = new Set([
    "Filter by year",
    "Filter by status",
    "Filter by member",
    "Filter by department",
    "Service",
    "Search services",
    "Clear service",
    "bonus-person",
    "bonus-person-err",
    "bonus-amount",
    "bonus-amount-err",
    "bonus-date",
    "bonus-date-err",
    "bonus-reason",
    "bonus-reason-err",
  ]);
  const opaque = (v: string) => /^(radix-)?_?r_/.test(v);

  /** Every personal value of the fixtures and of the typed form, raw and formatted. */
  const PERSONAL = [
    "Fake Setup",
    "Fake Tax One",
    "Fake Tax Two",
    "Fake Office",
    "fake.setup@example.com",
    "fake.office@example.com",
    "fake.admin@example.com",
    "Weekend filing",
    "Already approved extra",
    "Contoh alasan unik",
    "1234567",
    "1.234.567",
    "600000",
    "600.000",
    "200000",
    "200.000",
    "1000000",
    "1.000.000",
    "60%",
    "20%",
  ];

  function allAttributes(region: Element) {
    return Array.from(region.querySelectorAll("*")).flatMap((el) =>
      el.getAttributeNames().map((name) => ({
        el,
        name,
        value: el.getAttribute(name) ?? "",
      })),
    );
  }

  it("never puts a person's name or personal value in ANY attribute of the protected regions", async () => {
    const user = userEvent.setup();
    render(<BonusesPage />);
    await openPeople(user);
    await user.click(await screen.findByRole("button", { name: "Add bonus" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Person"), "3");
    await user.type(within(dialog).getByLabelText("Amount (IDR)"), "1234567");
    await user.type(
      within(dialog).getByLabelText(/Reason/),
      "Contoh alasan unik",
    );
    // the typed values are really in the form, as properties, not attributes
    expect(
      (within(dialog).getByLabelText("Amount (IDR)") as HTMLInputElement).value,
    ).toBe("1234567");
    expect(
      (within(dialog).getByLabelText(/Reason/) as HTMLTextAreaElement).value,
    ).toBe("Contoh alasan unik");

    // the page region and the portaled dialog are each protected on their own
    const regions = document.querySelectorAll("[data-sentry-block]");
    expect(regions.length).toBe(2);
    expect(
      document.querySelector("[role=dialog] [data-sentry-block]"),
    ).not.toBeNull();

    for (const region of regions) {
      const attrs = allAttributes(region);
      expect(attrs.length).toBeGreaterThan(50);
      // 1. no attribute value contains any personal value (case-insensitive)
      for (const { name, value, el } of attrs) {
        for (const secret of PERSONAL) {
          expect(
            value.toLowerCase().includes(secret.toLowerCase()),
            `${el.tagName} ${name}="${value}" carries ${secret}`,
          ).toBe(false);
        }
      }
      // 2. the five attributes Sentry records hold only fixed UI strings
      const recorded = attrs
        .filter((a) => RECORDED.includes(a.name))
        .map((a) => a.value)
        .filter((v) => !FIXED_UI_STRINGS.has(v) && !opaque(v));
      expect(recorded).toEqual([]);
    }
    // names, amounts and reasons stay reachable as text
    expect(screen.getAllByText(/Fake Tax Two/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Weekend filing/).length).toBeGreaterThan(0);
  });

  it("wraps the whole page, tables and bonus rows included", async () => {
    render(<BonusesPage />);
    const table = await screen.findByRole("table");
    expect(table.closest("[data-sentry-block]")).not.toBeNull();
  });

  it("clears the typed values when the dialog is closed and reopened", async () => {
    const user = userEvent.setup();
    render(<BonusesPage />);
    await user.click(await screen.findByRole("button", { name: "Add bonus" }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Amount (IDR)"), "777");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(screen.getByRole("button", { name: "Add bonus" }));
    dialog = await screen.findByRole("dialog");
    expect(
      (within(dialog).getByLabelText("Amount (IDR)") as HTMLInputElement).value,
    ).toBe("");
  });
});
