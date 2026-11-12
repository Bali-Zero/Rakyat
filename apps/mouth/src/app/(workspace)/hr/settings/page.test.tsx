import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HRSettingsPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listBonusRates: vi.fn(),
  upsertBonusRate: vi.fn(),
  listBonusPracticeTypes: vi.fn(),
}));
vi.mock("@/lib/api/hr/hr", () => hrApiMock);
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const TYPES = [
  { code: "visa_fake", name: "Fake visa", category: "visa", group: "visa" },
  {
    code: "company_fake",
    name: "Fake company",
    category: "company",
    group: "company",
  },
  {
    code: "tax_fake_report",
    name: "Fake annual report",
    category: "tax",
    group: "tax",
  },
  {
    code: "tax_fake_vat",
    name: "Fake VAT filing",
    category: "tax",
    group: "tax",
  },
];

function rate(
  id: number,
  code: string,
  name: string,
  amount: number,
  active = true,
  extra: Record<string, unknown> = {},
) {
  return {
    id,
    practice_type_code: code,
    practice_type_name: name,
    amount_idr: amount,
    effective_from: "2026-01-01",
    effective_to: null,
    is_active: active,
    notes: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...extra,
  };
}

// A rate with every field an upsert could blank: notes and both dates.
const VISA_EXTRA = {
  notes: "Fake agreed note",
  effective_from: "2026-03-01",
  effective_to: "2026-12-31",
};

beforeEach(() => {
  vi.clearAllMocks();
  hrApiMock.listBonusRates.mockResolvedValue({
    rates: [
      rate(1, "visa_fake", "Fake visa", 500_000, true, VISA_EXTRA),
      rate(2, "company_fake", "Fake company", 750_000, false),
    ],
    count: 2,
  });
  hrApiMock.listBonusPracticeTypes.mockResolvedValue({
    practice_types: TYPES,
    count: 4,
  });
});

describe("HR settings", () => {
  it("groups the rates, and shows the Tax group with an inviting CTA while it is empty", async () => {
    render(<HRSettingsPage />);
    const visa = await screen.findByRole("region", {
      name: "Visa & KITAS rates",
    });
    expect(within(visa).getByText("Fake visa")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Company rates" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Other rates" })).toBeNull();
    const tax = screen.getByRole("region", { name: "Tax rates" });
    expect(tax).toHaveTextContent("Tax services");
    expect(tax).toHaveTextContent("No tax rate yet");
    expect(
      within(tax).getByRole("button", { name: /Add a tax rate/ }),
    ).toBeInTheDocument();
  });

  it("the CTA opens the form on tax services only and creates the rate", async () => {
    hrApiMock.upsertBonusRate.mockResolvedValue({
      success: true,
      rate: rate(9, "tax_fake_report", "", 300_000),
    });
    const user = userEvent.setup();
    render(<HRSettingsPage />);
    await user.click(
      await screen.findByRole("button", { name: /Add a tax rate/ }),
    );
    await user.click(
      screen.getByRole("combobox", { name: "Service for the new rate" }),
    );
    const list = screen.getByRole("listbox");
    expect(
      within(list)
        .getAllByRole("group")
        .map((g) => g.getAttribute("aria-label")),
    ).toEqual(["Tax"]);
    await user.click(
      within(list).getByRole("option", { name: "Fake annual report" }),
    );
    await user.type(screen.getByLabelText("Amount (IDR)"), "300000");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(hrApiMock.upsertBonusRate).toHaveBeenCalledWith({
        practice_type_code: "tax_fake_report",
        amount_idr: 300000,
      }),
    );
    const tax = await screen.findByRole("region", { name: "Tax rates" });
    await waitFor(() =>
      expect(within(tax).getByText("Fake annual report")).toBeInTheDocument(),
    );
    expect(tax).not.toHaveTextContent("No tax rate yet");
  });

  it("the full dropdown is grouped Visa, Company, Tax, searchable, and greys out services that have a rate", async () => {
    const user = userEvent.setup();
    render(<HRSettingsPage />);
    await user.click(await screen.findByRole("button", { name: "Add Rate" }));
    await user.click(
      screen.getByRole("combobox", { name: "Service for the new rate" }),
    );
    const list = screen.getByRole("listbox");
    expect(
      within(list)
        .getAllByRole("group")
        .map((g) => g.getAttribute("aria-label")),
    ).toEqual(["Visa & KITAS", "Company", "Tax"]);
    expect(
      within(list).getByRole("option", { name: /Fake visa/ }),
    ).toBeDisabled();
    await user.type(screen.getByLabelText("Search services"), "vat");
    expect(
      within(screen.getByRole("listbox")).getAllByRole("option"),
    ).toHaveLength(1);
  });

  it("an inline amount edit and an active toggle send notes and both dates back unchanged", async () => {
    // the server echoes what it stored, as the real upsert does
    hrApiMock.upsertBonusRate.mockImplementation(async (p) => ({
      success: true,
      rate: rate(1, p.practice_type_code, "", p.amount_idr, p.is_active, {
        notes: p.notes ?? null,
        effective_from: p.effective_from ?? null,
        effective_to: p.effective_to ?? null,
      }),
    }));
    const user = userEvent.setup();
    render(<HRSettingsPage />);
    await user.click(
      await screen.findByRole("button", { name: "Edit amount for Fake visa" }),
    );
    const input = screen.getByLabelText("New amount");
    await user.clear(input);
    await user.type(input, "650000");
    await user.click(screen.getByRole("button", { name: "Save amount" }));
    await waitFor(() =>
      expect(hrApiMock.upsertBonusRate).toHaveBeenCalledTimes(1),
    );
    expect(hrApiMock.upsertBonusRate).toHaveBeenLastCalledWith({
      practice_type_code: "visa_fake",
      amount_idr: 650000,
      effective_from: "2026-03-01",
      effective_to: "2026-12-31",
      is_active: true,
      notes: "Fake agreed note",
    });

    await user.click(screen.getByRole("switch", { name: "Fake visa active" }));
    await waitFor(() =>
      expect(hrApiMock.upsertBonusRate).toHaveBeenCalledTimes(2),
    );
    expect(hrApiMock.upsertBonusRate).toHaveBeenLastCalledWith({
      practice_type_code: "visa_fake",
      amount_idr: 650000,
      effective_from: "2026-03-01",
      effective_to: "2026-12-31",
      is_active: false,
      notes: "Fake agreed note",
    });
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Fake visa active" }),
      ).toHaveAttribute("aria-checked", "false"),
    );
  });
});
