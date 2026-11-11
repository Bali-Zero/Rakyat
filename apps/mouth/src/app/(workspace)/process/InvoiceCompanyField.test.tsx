import React, { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error-handler";
import {
  EMPTY_INVOICE_COMPANY,
  InvoiceCompanyField,
  ambiguityFromError,
  invoiceCompanyParams,
  type InvoiceCompanyValue,
} from "./InvoiceCompanyField";

const { mockSuggest } = vi.hoisted(() => ({ mockSuggest: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { crm: { getInvoiceCompanySuggestion: mockSuggest } },
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));

const choice = (id: number, name: string, director = false) => ({
  id,
  company_name: name,
  npwp_company: null,
  role: null,
  is_director: director,
});

function Harness({
  clientId = 7,
  code = "tax_annual_company",
  autofill,
  initial = EMPTY_INVOICE_COMPANY,
  onValue,
}: {
  clientId?: number;
  code?: string;
  autofill?: boolean;
  initial?: InvoiceCompanyValue;
  onValue: (v: InvoiceCompanyValue) => void;
}) {
  const [value, setValue] = useState(initial);
  onValue(value);
  return (
    <InvoiceCompanyField
      clientId={clientId}
      practiceTypeCode={code}
      value={value}
      onChange={setValue}
      autofill={autofill}
    />
  );
}

describe("InvoiceCompanyField", () => {
  beforeEach(() => vi.clearAllMocks());

  it("prefills the single linked company the backend suggests", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    render(<Harness onValue={seen} />);
    const select = await screen.findByRole("combobox", {
      name: "Invoice to company",
    });
    await waitFor(() => expect(select).toHaveValue("11"));
    expect(mockSuggest).toHaveBeenCalledWith(7, "tax_annual_company");
  });

  it("offers a choice with several companies and does not pick one unasked", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [
        choice(1, "PT Synthetic A"),
        choice(2, "PT Synthetic B", true),
      ],
    });
    render(<Harness onValue={vi.fn()} />);
    const select = await screen.findByRole("combobox", {
      name: "Invoice to company",
    });
    expect(select).toHaveValue("");
    expect(screen.getByText("PT Synthetic B (director)")).toBeInTheDocument();
  });

  it("does not prefill when the backend says autofill is off for this service", async () => {
    mockSuggest.mockResolvedValue({
      autofill: false,
      suggested_company_id: null,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    render(<Harness code="kitas_working_onshore" onValue={vi.fn()} />);
    const select = await screen.findByRole("combobox", {
      name: "Invoice to company",
    });
    expect(select).toHaveValue("");
  });

  it("clears a prefill when the service changes to one without autofill", async () => {
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    mockSuggest.mockResolvedValueOnce({
      autofill: false,
      suggested_company_id: null,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() =>
      expect(seen).toHaveBeenLastCalledWith(
        expect.objectContaining({ companyId: 11 }),
      ),
    );
    rerender(<Harness code="kitas_working_onshore" onValue={seen} />);
    await waitFor(() =>
      expect(seen).toHaveBeenLastCalledWith(EMPTY_INVOICE_COMPANY),
    );
  });

  it("shows free-text inputs when the client has no company and reports the typed name", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    const seen = vi.fn();
    const user = userEvent.setup();
    render(<Harness onValue={seen} />);
    await user.type(
      await screen.findByLabelText("Company name"),
      "PT Synthetic Baru",
    );
    await user.type(screen.getByLabelText("Company NPWP"), "01.234");
    const last = seen.mock.calls[
      seen.mock.calls.length - 1
    ][0] as InvoiceCompanyValue;
    expect(invoiceCompanyParams(last)).toEqual({
      invoice_company_name: "PT Synthetic Baru",
      invoice_company_npwp: "01.234",
    });
    expect(
      screen.getByText(/creates the company and links it to this client/),
    ).toBeInTheDocument();
  });

  it("clear empties the prefill and it is not refilled", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    const user = userEvent.setup();
    render(<Harness onValue={seen} />);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("11"));
    await user.click(
      screen.getByRole("button", { name: "Clear invoice company" }),
    );
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(
      invoiceCompanyParams(seen.mock.calls[seen.mock.calls.length - 1][0]),
    ).toEqual({});
  });

  it("a cleared prefill is not filled again when the service changes", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("11"));
    await user.click(
      screen.getByRole("button", { name: "Clear invoice company" }),
    );
    rerender(<Harness code="pt_pma_setup" onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("combobox")).toHaveValue("");
  });

  it.each([
    ["pending", () => new Promise(() => {})],
    ["rejected", () => Promise.reject(new Error("down"))],
  ])(
    "a tax prefill is dropped at once when the service changes (%s suggestion)",
    async (_label, next) => {
      mockSuggest.mockResolvedValueOnce({
        autofill: true,
        suggested_company_id: 11,
        companies: [choice(11, "PT Synthetic Satu")],
      });
      mockSuggest.mockImplementationOnce(next);
      const seen = vi.fn();
      const { rerender } = render(<Harness onValue={seen} />);
      await waitFor(() =>
        expect(seen).toHaveBeenLastCalledWith(
          expect.objectContaining({ companyId: 11 }),
        ),
      );
      rerender(<Harness code="kitas_working_onshore" onValue={seen} />);
      await waitFor(() =>
        expect(seen).toHaveBeenLastCalledWith(EMPTY_INVOICE_COMPANY),
      );
    },
  );

  it("a name typed after a failed suggestion stays visible when a later suggestion succeeds", async () => {
    mockSuggest.mockRejectedValueOnce(new Error("down"));
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: null,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Harness onValue={seen} />);
    await user.type(
      await screen.findByLabelText("Company name"),
      "PT Synthetic Ketik",
    );
    rerender(<Harness code="pt_pma_setup" onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("combobox")).toHaveValue("__new__");
    expect(screen.getByLabelText("Company name")).toHaveValue(
      "PT Synthetic Ketik",
    );
    expect(
      invoiceCompanyParams(seen.mock.calls[seen.mock.calls.length - 1][0]),
    ).toEqual({
      invoice_company_name: "PT Synthetic Ketik",
    });
  });

  it("a prefill never survives a change of client", async () => {
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    const seen = vi.fn();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() =>
      expect(seen).toHaveBeenLastCalledWith(
        expect.objectContaining({ companyId: 11 }),
      ),
    );
    rerender(<Harness clientId={8} onValue={seen} />);
    await waitFor(() =>
      expect(seen).toHaveBeenLastCalledWith(EMPTY_INVOICE_COMPANY),
    );
  });

  it("never overwrites a saved company when autofill is off", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 2,
      companies: [choice(1, "PT Synthetic A"), choice(2, "PT Synthetic B")],
    });
    render(
      <Harness
        autofill={false}
        initial={{ companyId: 1, name: "", npwp: "" }}
        onValue={vi.fn()}
      />,
    );
    const select = await screen.findByRole("combobox");
    await waitFor(() => expect(mockSuggest).toHaveBeenCalled());
    expect(select).toHaveValue("1");
  });

  it("a failed suggestion leaves the field usable", async () => {
    mockSuggest.mockRejectedValue(new Error("down"));
    render(<Harness onValue={vi.fn()} />);
    expect(await screen.findByLabelText("Company name")).toBeInTheDocument();
  });
});

describe("invoice company helpers", () => {
  it("payload is the id, else the typed name, else nothing", () => {
    expect(
      invoiceCompanyParams({ companyId: 3, name: "ignored", npwp: "" }),
    ).toEqual({
      invoice_company_id: 3,
    });
    expect(
      invoiceCompanyParams({ companyId: null, name: "  ", npwp: "x" }),
    ).toEqual({});
  });

  it("recognises the 409 candidates and nothing else", () => {
    const body = {
      detail: {
        code: "invoice_company_ambiguous",
        candidates: [{ id: 1, company_name: "PT Synthetic Kembar" }],
      },
    };
    expect(ambiguityFromError(new ApiError("x", 409, body), "Kembar")).toEqual({
      typed: "Kembar",
      candidates: body.detail.candidates,
    });
    expect(
      ambiguityFromError(new ApiError("x", 409, { detail: "other" }), "a"),
    ).toBeNull();
    expect(ambiguityFromError(new ApiError("x", 500, body), "a")).toBeNull();
    expect(
      ambiguityFromError(
        new ApiError("x", 409, {
          detail: { code: "other", candidates: body.detail.candidates },
        }),
        "a",
      ),
    ).toBeNull();
    expect(ambiguityFromError(new Error("x"), "a")).toBeNull();
  });
});
