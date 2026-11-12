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

  it('an empty "Another company" draft is not a change and never sends a Clear', async () => {
    const user = userEvent.setup();
    render(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
        })}
        onSaved={vi.fn()}
      />,
    );
    await user.selectOptions(await screen.findByRole("combobox"), "__new__");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("choosing No company is an explicit clear and sends null", async () => {
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
    await user.selectOptions(await screen.findByRole("combobox"), "");
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

  it("while a save is in flight Save and the field are disabled, so saves never overlap", async () => {
    const user = userEvent.setup();
    let resolveSave: (p: Practice) => void = () => {};
    mockUpdate.mockReturnValue(
      new Promise<Practice>((resolve) => {
        resolveSave = resolve;
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />);
    const select = await screen.findByRole("combobox");
    await user.selectOptions(select, "2");
    const save = screen.getByRole("button", { name: "Save" });
    await user.click(save);
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    expect(save).toBeDisabled();
    expect(select).toBeDisabled();
    await user.click(save);
    expect(mockUpdate).toHaveBeenCalledTimes(1);
    resolveSave(practice({ invoice_company_id: 2 }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
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
    expect(screen.getByRole("combobox")).toHaveValue("1");
    mockUpdate.mockResolvedValueOnce(practice({ invoice_company_id: 1 }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(2));
    expect(mockUpdate).toHaveBeenLastCalledWith(5, { invoice_company_id: 1 });
  });

  it("editing after a 409 withdraws the stale candidates", async () => {
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
          candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
        },
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />);
    const name = await screen.findByLabelText("Company name");
    await user.type(name, "Synthetic Kembar");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByTestId("invoice-company-ambiguity");
    await user.type(name, " Dua");
    expect(screen.queryByTestId("invoice-company-ambiguity")).toBeNull();
    expect(mockUpdate).toHaveBeenCalledTimes(1);
  });

  it("an NPWP without a name cannot be saved and never becomes a Clear", async () => {
    const user = userEvent.setup();
    render(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
        })}
        onSaved={vi.fn()}
      />,
    );
    await user.selectOptions(await screen.findByRole("combobox"), "__new__");
    await user.type(screen.getByLabelText("Company NPWP"), "01.234");
    expect(screen.getByRole("alert")).toHaveTextContent("company name");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("onSaved carries only the invoice fields", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    mockUpdate.mockResolvedValue(
      practice({
        priority: "urgent",
        invoice_company_id: 2,
        invoice_company_name: "PT Synthetic B",
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={onSaved} />);
    await user.selectOptions(await screen.findByRole("combobox"), "2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onSaved).toHaveBeenCalledWith({
      invoice_company_id: 2,
      invoice_company_name: "PT Synthetic B",
    });
  });

  it("a failed direct candidate save keeps the pick, and Save retries it", async () => {
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
          candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
        },
      }),
    );
    mockUpdate.mockRejectedValueOnce(new Error("boom"));
    mockUpdate.mockResolvedValueOnce(
      practice({
        invoice_company_id: 8,
        invoice_company_name: "PT Synthetic Kembar",
      }),
    );
    render(<InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />);
    await user.type(
      await screen.findByLabelText("Company name"),
      "Synthetic Kembar",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(
      await screen.findByRole("button", { name: "PT Synthetic Kembar" }),
    );
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(await screen.findByRole("combobox")).toHaveValue("8");
    expect(
      screen.getByRole("option", { name: "PT Synthetic Kembar" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(3));
    expect(mockUpdate).toHaveBeenLastCalledWith(5, { invoice_company_id: 8 });
  });

  it("follows a company saved elsewhere while clean, but never erases a draft", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [choice(1, "PT Synthetic A"), choice(2, "PT Synthetic B")],
    });
    const user = userEvent.setup();
    const { rerender } = render(
      <InvoiceCompanyCard practice={practice()} onSaved={vi.fn()} />,
    );
    await screen.findByRole("combobox");
    rerender(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 2,
          invoice_company_name: "PT Synthetic B",
        })}
        onSaved={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("2"));
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    await user.selectOptions(screen.getByRole("combobox"), "1");
    rerender(
      <InvoiceCompanyCard
        practice={practice({ invoice_company_id: null })}
        onSaved={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox")).toHaveValue("1");
  });

  it("a saved prefill is not cleared by a later change of service", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 1,
      companies: [choice(1, "PT Synthetic A")],
    });
    const user = userEvent.setup();
    mockUpdate.mockResolvedValue(
      practice({
        invoice_company_id: 1,
        invoice_company_name: "PT Synthetic A",
      }),
    );
    const onSaved = vi.fn();
    const { rerender } = render(
      <InvoiceCompanyCard practice={practice()} onSaved={onSaved} />,
    );
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("1"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    rerender(
      <InvoiceCompanyCard
        practice={practice({
          invoice_company_id: 1,
          invoice_company_name: "PT Synthetic A",
          practice_type_code: "kitas_working_onshore",
        })}
        onSaved={onSaved}
      />,
    );
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("combobox")).toHaveValue("1");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("a chooser is withdrawn when the service changes, and a late 409 for the old service is ignored", async () => {
    const user = userEvent.setup();
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    const ambiguous = new ApiError("conflict", 409, {
      detail: {
        code: "invoice_company_ambiguous",
        candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
      },
    });
    mockUpdate.mockRejectedValueOnce(ambiguous);
    let rejectLate: (e: unknown) => void = () => {};
    mockUpdate.mockReturnValueOnce(
      new Promise((_r, reject) => (rejectLate = reject)),
    );
    const onSaved = vi.fn();
    const { rerender } = render(
      <InvoiceCompanyCard practice={practice()} onSaved={onSaved} />,
    );
    const name = await screen.findByLabelText("Company name");
    await user.type(name, "Synthetic Kembar");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByTestId("invoice-company-ambiguity");
    rerender(
      <InvoiceCompanyCard
        practice={practice({ practice_type_code: "kitas_working_onshore" })}
        onSaved={onSaved}
      />,
    );
    expect(screen.queryByTestId("invoice-company-ambiguity")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(2));
    rerender(
      <InvoiceCompanyCard
        practice={practice({ practice_type_code: "tax" })}
        onSaved={onSaved}
      />,
    );
    rejectLate(ambiguous);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save" })).toBeEnabled(),
    );
    expect(screen.queryByTestId("invoice-company-ambiguity")).toBeNull();
  });
});
