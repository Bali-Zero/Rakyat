import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error-handler";
import type { Practice } from "@/lib/api/crm/crm.types";
import { InvoiceCompanyCard } from "./InvoiceCompanyCard";

const { mockSuggest, mockUpdate, toastSuccess, toastError } = vi.hoisted(
  () => ({
    mockSuggest: vi.fn(),
    mockUpdate: vi.fn(),
    toastSuccess: vi.fn(),
    toastError: vi.fn(),
  }),
);

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getInvoiceCompanySuggestion: mockSuggest,
      updatePractice: mockUpdate,
    },
  },
}));
vi.mock("sonner", () => ({
  toast: { success: toastSuccess, error: toastError },
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));

const practice = (extra: Partial<Practice> = {}): Practice =>
  ({
    id: 5,
    client_id: 7,
    practice_type_code: "tax_annual_company",
    status: "inquiry",
    priority: "normal",
    payment_status: "unpaid",
    practice_type_id: 1,
    created_at: "2026-11-12T00:00:00Z",
    ...extra,
  }) as Practice;

const choice = (id: number, name: string) => ({
  id,
  company_name: name,
  npwp_company: null,
  role: null,
  is_director: false,
});

describe("InvoiceCompanyCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [choice(1, "PT Synthetic A"), choice(2, "PT Synthetic B")],
    });
  });

  it("saves a picked company by id", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    mockUpdate.mockResolvedValue(
      practice({
        invoice_company_id: 2,
        invoice_company_name: "PT Synthetic B",
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={onSaved} />);
    await user.selectOptions(await screen.findByRole("combobox"), "2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(mockUpdate).toHaveBeenCalledWith(5, { invoice_company_id: 2 });
  });

  it("clearing a saved company sends an explicit null", async () => {
    const user = userEvent.setup();
    mockUpdate.mockResolvedValue(practice());
    render(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
        })}
        onSaved={vi.fn()}
      />,
    );
    await user.click(
      await screen.findByRole("button", { name: "Clear invoice company" }),
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith(5, { invoice_company_id: null }),
    );
  });

  it("a saved company is never replaced by the suggestion", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 2,
      companies: [choice(1, "PT Synthetic A"), choice(2, "PT Synthetic B")],
    });
    render(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
        })}
        onSaved={vi.fn()}
      />,
    );
    const select = await screen.findByRole("combobox");
    await waitFor(() => expect(mockSuggest).toHaveBeenCalled());
    expect(select).toHaveValue("1");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("shows no Save button until something changed", async () => {
    render(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
        })}
        onSaved={vi.fn()}
      />,
    );
    await screen.findByRole("combobox");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("turns a 409 into a chooser, never a raw error, and saves the pick", async () => {
    const user = userEvent.setup();
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    mockUpdate.mockRejectedValueOnce(
      new ApiError("conflict", 409, {
        detail: {
          code: "invoice_company_ambiguous",
          candidates: [
            { id: 8, company_name: "PT Synthetic Kembar" },
            { id: 9, company_name: "CV Synthetic Kembar" },
          ],
        },
      }),
    );
    mockUpdate.mockResolvedValueOnce(
      practice({
        invoice_company_id: 9,
        invoice_company_name: "CV Synthetic Kembar",
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />);
    await user.type(
      await screen.findByLabelText("Company name"),
      "Synthetic Kembar",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByTestId("invoice-company-ambiguity"),
    ).toBeInTheDocument();
    expect(toastError).not.toHaveBeenCalled();
    expect(mockUpdate).toHaveBeenLastCalledWith(5, {
      invoice_company_name: "Synthetic Kembar",
    });
    await user.click(
      screen.getByRole("button", { name: "CV Synthetic Kembar" }),
    );
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenLastCalledWith(5, { invoice_company_id: 9 }),
    );
  });

  it("other save errors surface as a toast", async () => {
    const user = userEvent.setup();
    mockUpdate.mockRejectedValue(new Error("boom"));
    render(<InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />);
    await user.selectOptions(await screen.findByRole("combobox"), "1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
  });
});
