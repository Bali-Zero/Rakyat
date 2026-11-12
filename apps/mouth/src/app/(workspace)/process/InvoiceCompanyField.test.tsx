import React, { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/error-handler";
import {
  EMPTY_INVOICE_COMPANY,
  InvoiceCompanyField,
  ambiguityFromError,
  invoiceCompanyIncomplete,
  invoiceCompanyParams,
  type InvoiceCompanyValue,
} from "./InvoiceCompanyField";

const { mockSuggest, mockSearch } = vi.hoisted(() => ({
  mockSuggest: vi.fn(),
  mockSearch: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getInvoiceCompanySuggestion: mockSuggest,
      searchInvoiceCompanies: mockSearch,
    },
  },
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
  setterRef,
  disabled,
}: {
  disabled?: boolean;
  setterRef?: { current: ((v: InvoiceCompanyValue) => void) | null };
  clientId?: number;
  code?: string;
  autofill?: boolean;
  initial?: InvoiceCompanyValue;
  onValue: (v: InvoiceCompanyValue) => void;
}) {
  const [value, setValue] = useState(initial);
  if (setterRef) setterRef.current = setValue;
  onValue(value);
  return (
    <InvoiceCompanyField
      clientId={clientId}
      practiceTypeCode={code}
      value={value}
      onChange={setValue}
      autofill={autofill}
      disabled={disabled}
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

  it("an NPWP typed before a slow suggestion resolves is never overwritten by it", async () => {
    let resolveSecond: (v: unknown) => void = () => {};
    mockSuggest.mockRejectedValueOnce(new Error("down"));
    mockSuggest.mockImplementationOnce(
      () => new Promise((resolve) => (resolveSecond = resolve)),
    );
    const seen = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Harness onValue={seen} />);
    const npwp = await screen.findByLabelText("Company NPWP");
    rerender(<Harness code="pt_pma_setup" onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    await user.type(npwp, "01.234");
    resolveSecond({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    await screen.findByRole("combobox");
    const last = seen.mock.calls[
      seen.mock.calls.length - 1
    ][0] as InvoiceCompanyValue;
    expect(last).toEqual({ companyId: null, name: "", npwp: "01.234" });
    expect(screen.getByLabelText("Company NPWP")).toHaveValue("01.234");
  });

  it("a company picked by the parent (409 chooser) is not replaced by a later suggestion", async () => {
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: 11,
      companies: [
        choice(11, "PT Synthetic Satu"),
        choice(5, "PT Synthetic Lima"),
      ],
    });
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: 11,
      companies: [
        choice(11, "PT Synthetic Satu"),
        choice(5, "PT Synthetic Lima"),
      ],
    });
    const seen = vi.fn();
    const setterRef = {
      current: null as ((v: InvoiceCompanyValue) => void) | null,
    };
    const { rerender } = render(
      <Harness onValue={seen} setterRef={setterRef} />,
    );
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("11"));
    act(() => setterRef.current!({ companyId: 5, name: "", npwp: "" }));
    rerender(
      <Harness code="pt_pma_setup" onValue={seen} setterRef={setterRef} />,
    );
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("combobox")).toHaveValue("5");
  });

  it("the previous client's companies are not selectable after the client changes", async () => {
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: null,
      companies: [
        choice(1, "PT Synthetic Lama"),
        choice(2, "PT Synthetic Lama Dua"),
      ],
    });
    mockSuggest.mockImplementationOnce(() => new Promise(() => {}));
    const { rerender } = render(<Harness onValue={vi.fn()} />);
    await screen.findByRole("combobox");
    rerender(<Harness clientId={8} onValue={vi.fn()} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText("PT Synthetic Lama")).toBeNull();
  });

  it("a suggestion for the previous client that resolves late restores nothing", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSuggest.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    mockSuggest.mockResolvedValueOnce({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    const seen = vi.fn();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(1));
    rerender(<Harness clientId={8} onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveOld({
        autofill: true,
        suggested_company_id: 11,
        companies: [choice(11, "PT Synthetic Lama")],
      });
    });
    expect(seen).not.toHaveBeenCalledWith(
      expect.objectContaining({ companyId: 11 }),
    );
    expect(screen.queryByText("PT Synthetic Lama")).toBeNull();
  });

  it("a suggestion for the previous service that resolves late restores nothing", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSuggest.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    mockSuggest.mockResolvedValueOnce({
      autofill: false,
      suggested_company_id: null,
      companies: [],
    });
    const seen = vi.fn();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(1));
    rerender(<Harness code="kitas_working_onshore" onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveOld({
        autofill: true,
        suggested_company_id: 11,
        companies: [choice(11, "PT Synthetic Lama")],
      });
    });
    expect(seen).not.toHaveBeenCalledWith(
      expect.objectContaining({ companyId: 11 }),
    );
  });

  it("a prefill that was saved is not cleared by a later service change", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    const seen = vi.fn();
    const { rerender } = render(<Harness onValue={seen} />);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("11"));
    rerender(<Harness autofill={false} onValue={seen} />);
    rerender(
      <Harness autofill={false} code="kitas_working_onshore" onValue={seen} />,
    );
    await waitFor(() => expect(mockSuggest).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("combobox")).toHaveValue("11");
  });

  it("typed inputs disappear when the parent sets a company", async () => {
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [choice(5, "PT Synthetic Lima")],
    });
    const setterRef = {
      current: null as ((v: InvoiceCompanyValue) => void) | null,
    };
    const user = userEvent.setup();
    render(<Harness onValue={vi.fn()} setterRef={setterRef} />);
    await user.selectOptions(await screen.findByRole("combobox"), "__new__");
    expect(screen.getByLabelText("Company name")).toBeInTheDocument();
    act(() => setterRef.current!({ companyId: 5, name: "", npwp: "" }));
    expect(screen.queryByLabelText("Company name")).toBeNull();
    expect(screen.getByRole("combobox")).toHaveValue("5");
  });

  it("a suggestion that arrives while the field is disabled does not write", async () => {
    let resolveIt: (v: unknown) => void = () => {};
    mockSuggest.mockImplementationOnce(
      () => new Promise((r) => (resolveIt = r)),
    );
    const seen = vi.fn();
    render(<Harness disabled onValue={seen} />);
    await waitFor(() => expect(mockSuggest).toHaveBeenCalled());
    resolveIt({
      autofill: true,
      suggested_company_id: 11,
      companies: [choice(11, "PT Synthetic Satu")],
    });
    await screen.findByRole("combobox");
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(
      invoiceCompanyParams(seen.mock.calls[seen.mock.calls.length - 1][0]),
    ).toEqual({});
  });

  it("the 409 candidates cannot be picked while the field is busy", () => {
    const pick = vi.fn();
    render(
      <InvoiceCompanyField
        clientId={7}
        practiceTypeCode="tax"
        value={EMPTY_INVOICE_COMPANY}
        onChange={vi.fn()}
        disabled
        ambiguity={{
          typed: "Kembar",
          candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
        }}
        onPickCandidate={pick}
      />,
    );
    expect(
      screen.getByRole("button", { name: "PT Synthetic Kembar" }),
    ).toBeDisabled();
  });

  it("an NPWP without a name is an incomplete draft", () => {
    expect(
      invoiceCompanyIncomplete({ companyId: null, name: " ", npwp: "01.2" }),
    ).toBe(true);
    expect(
      invoiceCompanyIncomplete({ companyId: null, name: "PT X", npwp: "01.2" }),
    ).toBe(false);
    expect(invoiceCompanyIncomplete({ companyId: 3, name: "", npwp: "" })).toBe(
      false,
    );
    expect(invoiceCompanyIncomplete(EMPTY_INVOICE_COMPANY)).toBe(false);
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
    expect(
      invoiceCompanyParams({
        companyId: null,
        name: "PT Synthetic Baru",
        npwp: "",
      }),
    ).toEqual({ invoice_company_name: "PT Synthetic Baru" });
    expect(
      invoiceCompanyParams({
        companyId: null,
        name: " PT Synthetic Baru ",
        npwp: "01.234.567.8-901.000",
      }),
    ).toEqual({
      invoice_company_name: "PT Synthetic Baru",
      invoice_company_npwp: "01.234.567.8-901.000",
    });
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

describe("InvoiceCompanyField live company search", () => {
  const match = (id: number, name: string, linked = false) => ({
    id,
    company_name: name,
    linked,
  });
  const settle = (ms = 400) =>
    act(async () => {
      await new Promise((resolve) => setTimeout(resolve, ms));
    });

  beforeEach(() => {
    vi.clearAllMocks();
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    mockSearch.mockReset();
    mockSearch.mockResolvedValue({ companies: [] });
  });

  async function typeName(text: string) {
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Company name"), text);
    return user;
  }

  it("fewer than two characters never searches", async () => {
    render(<Harness onValue={vi.fn()} />);
    await typeName("b");
    await settle();
    expect(mockSearch).not.toHaveBeenCalled();
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
  });

  it("two or more characters search once, debounced, and list the matches then Create", async () => {
    mockSearch.mockResolvedValue({
      companies: [
        match(21, "PT BAYU BALI NOL", true),
        match(22, "PT Bayu Santero"),
      ],
    });
    render(<Harness onValue={vi.fn()} />);
    await typeName("bayu");
    const list = await screen.findByTestId("invoice-company-search");
    expect(mockSearch).toHaveBeenCalledTimes(1);
    expect(mockSearch).toHaveBeenCalledWith(7, "bayu");
    const buttons = Array.from(list.querySelectorAll("button")).map(
      (b) => b.textContent,
    );
    expect(buttons).toEqual([
      "PT BAYU BALI NOLlinked to this client",
      "PT Bayu Santero",
      "Create «bayu»",
    ]);
  });

  it("picking an existing company sets its id, creates nothing and shows its name", async () => {
    mockSearch.mockResolvedValue({
      companies: [match(21, "PT BAYU BALI NOL")],
    });
    const seen = vi.fn();
    render(<Harness onValue={seen} />);
    const user = await typeName("bayu");
    await user.click(
      await screen.findByRole("button", { name: "PT BAYU BALI NOL" }),
    );
    expect(seen).toHaveBeenLastCalledWith({
      companyId: 21,
      name: "",
      npwp: "",
    });
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
    expect(screen.queryByLabelText("Company name")).toBeNull();
    expect(
      screen.getByRole("option", { name: "PT BAYU BALI NOL" }),
    ).toBeInTheDocument();
  });

  it("Create keeps the typed name and closes the list until the name changes", async () => {
    mockSearch.mockResolvedValue({ companies: [match(21, "PT Bayu Lain")] });
    const seen = vi.fn();
    render(<Harness onValue={seen} />);
    const user = await typeName("bayu");
    await user.click(
      await screen.findByRole("button", { name: "Create «bayu»" }),
    );
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ companyId: null, name: "bayu" }),
    );
    await user.type(screen.getByLabelText("Company name"), "x");
    expect(
      await screen.findByRole("button", { name: "Create «bayux»" }),
    ).toBeInTheDocument();
  });

  it("no match offers only Create", async () => {
    render(<Harness onValue={vi.fn()} />);
    await typeName("zzzz");
    const list = await screen.findByTestId("invoice-company-search");
    expect(
      Array.from(list.querySelectorAll("button")).map((b) => b.textContent),
    ).toEqual(["Create «zzzz»"]);
  });

  it("an older search that resolves after a newer one is never shown", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSearch.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    mockSearch.mockResolvedValueOnce({
      companies: [match(22, "PT Bayu Baru")],
    });
    render(<Harness onValue={vi.fn()} />);
    const user = await typeName("ba");
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    await user.type(screen.getByLabelText("Company name"), "yu");
    await screen.findByRole("button", { name: "PT Bayu Baru" });
    await act(async () => {
      resolveOld({ companies: [match(21, "PT Ba Lama")] });
    });
    expect(screen.queryByRole("button", { name: "PT Ba Lama" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "PT Bayu Baru" }),
    ).toBeInTheDocument();
  });

  it("a search for the previous client that resolves late shows nothing", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSearch.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    const { rerender } = render(<Harness onValue={vi.fn()} />);
    await typeName("bayu");
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    rerender(<Harness clientId={8} onValue={vi.fn()} />);
    await act(async () => {
      resolveOld({ companies: [match(21, "PT Bayu Lama")] });
    });
    expect(screen.queryByRole("button", { name: "PT Bayu Lama" })).toBeNull();
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
  });

  it("a failed search leaves typing usable and shows no list", async () => {
    mockSearch.mockRejectedValue(new Error("boom"));
    const seen = vi.fn();
    render(<Harness onValue={seen} />);
    await typeName("bayu");
    await waitFor(() => expect(mockSearch).toHaveBeenCalled());
    await settle(50);
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ name: "bayu" }),
    );
  });

  it("a disabled field neither searches nor shows a response that arrives while disabled", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSearch.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    const typed = { companyId: null, name: "bayu", npwp: "" };
    const { rerender } = render(<Harness initial={typed} onValue={vi.fn()} />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    rerender(<Harness initial={typed} disabled onValue={vi.fn()} />);
    await act(async () => {
      resolveOld({ companies: [match(21, "PT Bayu Lama")] });
    });
    expect(screen.queryByRole("button", { name: "PT Bayu Lama" })).toBeNull();
    await settle();
    expect(mockSearch).toHaveBeenCalledTimes(1);
  });

  it("the list of an older query is hidden while the newer query is still loading", async () => {
    mockSearch.mockResolvedValueOnce({ companies: [match(21, "PT Bay Lama")] });
    mockSearch.mockImplementationOnce(() => new Promise(() => {}));
    render(<Harness onValue={vi.fn()} />);
    const user = await typeName("bay");
    await screen.findByRole("button", { name: "PT Bay Lama" });
    await user.type(screen.getByLabelText("Company name"), "u");
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "PT Bay Lama" })).toBeNull();
  });

  it("after a pick from the list, a parent reset to empty does not reopen the typed inputs", async () => {
    mockSuggest.mockResolvedValue({
      autofill: false,
      suggested_company_id: null,
      companies: [choice(1, "PT Synthetic Satu")],
    });
    mockSearch.mockResolvedValue({ companies: [match(21, "PT Bayu Lain")] });
    const setterRef: { current: ((v: InvoiceCompanyValue) => void) | null } = {
      current: null,
    };
    render(<Harness setterRef={setterRef} onValue={vi.fn()} />);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox"), "__new__");
    await user.type(screen.getByLabelText("Company name"), "bayu");
    await user.click(
      await screen.findByRole("button", { name: "PT Bayu Lain" }),
    );
    act(() => setterRef.current?.(EMPTY_INVOICE_COMPANY));
    expect(screen.queryByLabelText("Company name")).toBeNull();
    expect(screen.getByRole("combobox")).toHaveValue("");
  });

  it("a search pending for the previous service that resolves late shows nothing", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    mockSearch.mockImplementationOnce(
      () => new Promise((resolve) => (resolveOld = resolve)),
    );
    mockSearch.mockResolvedValueOnce({ companies: [] });
    const typed = { companyId: null, name: "bayu", npwp: "" };
    const { rerender } = render(<Harness initial={typed} onValue={vi.fn()} />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    rerender(
      <Harness
        initial={typed}
        code="kitas_working_onshore"
        onValue={vi.fn()}
      />,
    );
    await act(async () => {
      resolveOld({ companies: [match(21, "PT Bayu Lama")] });
    });
    expect(screen.queryByRole("button", { name: "PT Bayu Lama" })).toBeNull();
  });

  it("a list shown for the previous service is withdrawn when the service changes", async () => {
    mockSearch.mockResolvedValueOnce({
      companies: [match(21, "PT Bayu Lama")],
    });
    mockSearch.mockImplementationOnce(() => new Promise(() => {}));
    const typed = { companyId: null, name: "bayu", npwp: "" };
    const { rerender } = render(<Harness initial={typed} onValue={vi.fn()} />);
    await screen.findByRole("button", { name: "PT Bayu Lama" });
    rerender(
      <Harness
        initial={typed}
        code="kitas_working_onshore"
        onValue={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "PT Bayu Lama" })).toBeNull();
  });

  it("editing the name away from a closed list and back reopens it", async () => {
    mockSearch.mockResolvedValue({ companies: [match(21, "PT Bayu Lain")] });
    render(<Harness onValue={vi.fn()} />);
    const user = await typeName("bayu");
    await user.click(
      await screen.findByRole("button", { name: "Create «bayu»" }),
    );
    expect(screen.queryByTestId("invoice-company-search")).toBeNull();
    await user.type(screen.getByLabelText("Company name"), "x");
    await user.type(screen.getByLabelText("Company name"), "{Backspace}");
    expect(
      await screen.findByRole("button", { name: "Create «bayu»" }),
    ).toBeInTheDocument();
  });

  it("after a service change the list is searched again for the new service", async () => {
    mockSearch.mockResolvedValueOnce({
      companies: [match(21, "PT Bayu Lama")],
    });
    mockSearch.mockResolvedValueOnce({
      companies: [match(22, "PT Bayu Baru")],
    });
    const typed = { companyId: null, name: "bayu", npwp: "" };
    const { rerender } = render(<Harness initial={typed} onValue={vi.fn()} />);
    await screen.findByRole("button", { name: "PT Bayu Lama" });
    rerender(
      <Harness
        initial={typed}
        code="kitas_working_onshore"
        onValue={vi.fn()}
      />,
    );
    expect(
      await screen.findByRole("button", { name: "PT Bayu Baru" }),
    ).toBeInTheDocument();
    expect(mockSearch).toHaveBeenCalledTimes(2);
  });
});
