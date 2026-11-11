import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { clientDetailQueryKey } from "@/hooks/useClientDetail";
import { ApiError } from "@/lib/api/error-handler";

const {
  mockPush,
  mockGetPracticeTypesCatalog,
  mockGetClient,
  mockGetFamilyMembers,
  mockGetClientPractices,
  mockCreatePractice,
  mockSuggest,
  mockGetClients,
  mockToastError,
  mockRefetchClientProfile,
  stableSearchParams,
} = vi.hoisted(() => ({
  mockPush: vi.fn(),
  mockGetPracticeTypesCatalog: vi.fn(),
  mockGetClient: vi.fn(),
  mockGetFamilyMembers: vi.fn(),
  mockGetClientPractices: vi.fn(),
  mockCreatePractice: vi.fn(),
  mockSuggest: vi.fn(),
  mockGetClients: vi.fn(),
  mockToastError: vi.fn(),
  mockRefetchClientProfile: vi.fn(),
  stableSearchParams: {
    get: vi.fn((key: string) => (key === "client_id" ? "7" : null)),
  },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  useSearchParams: () => stableSearchParams,
}));

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({
      email: "synthetic.team@example.test",
    }),
    isAdmin: vi.fn(() => true),
    crm: {
      getPracticeTypesCatalog: mockGetPracticeTypesCatalog,
      getClient: mockGetClient,
      getFamilyMembers: mockGetFamilyMembers,
      getClientPractices: mockGetClientPractices,
      createPractice: mockCreatePractice,
      getInvoiceCompanySuggestion: mockSuggest,
      getClients: mockGetClients,
    },
  },
}));

vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({
    options: [
      {
        value: "synthetic.team@example.test",
        label: "Synthetic Team Member",
      },
    ],
  }),
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({
    success: vi.fn(),
    error: mockToastError,
  }),
}));

vi.mock("@/components/partners/ReferrerDropdown", () => ({
  ReferrerDropdown: () => <div data-testid="referrer-dropdown" />,
}));

vi.mock("@/lib/api/partners/partners", () => ({
  createReferral: vi.fn(),
}));

vi.mock("@/lib/metrics/cases-metrics", () => ({
  casesMetrics: {
    trackPageView: vi.fn(),
    trackApiCall: vi.fn(),
    trackClientSearch: vi.fn(),
    trackError: vi.fn(),
    trackButtonClick: vi.fn(),
    startPerformanceMark: vi.fn(),
    trackCaseCreation: vi.fn(),
    endPerformanceMark: vi.fn(),
  },
}));

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn(), debug: vi.fn() },
}));

describe("NewPracticePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetPracticeTypesCatalog.mockResolvedValue({
      categories: [
        {
          code: "visa",
          label: "Visa",
          services: [
            {
              code: "SYNTHETIC_VISA",
              name: "Synthetic Visa Process",
              description: "Synthetic service for cache QA",
              base_price: null,
              typical_duration_days: 7,
            },
          ],
        },
      ],
    });
    mockGetClient.mockResolvedValue({
      id: 7,
      uuid: "synthetic-client-7",
      full_name: "Synthetic Process Client",
      email: "synthetic.process@example.test",
      status: "active",
      client_type: "individual",
      created_at: "2026-07-27T00:00:00Z",
      updated_at: "2026-07-27T00:00:00Z",
    });
    mockGetFamilyMembers.mockResolvedValue([]);
    mockGetClientPractices.mockResolvedValue([]);
    mockCreatePractice.mockResolvedValue({ id: 71 });
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: null,
      companies: [],
    });
    mockRefetchClientProfile.mockResolvedValue({
      client: { id: 7 },
      practices: [{ id: 71 }],
      stats: { practices_count: 1 },
    });
  });

  it("refetches the selected client before returning to its process tab", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = clientDetailQueryKey(7);
    queryClient.setQueryDefaults(queryKey, {
      queryFn: mockRefetchClientProfile,
    });
    queryClient.setQueryData(queryKey, { client: { id: 7 } });

    const { default: NewPracticePage } = await import("./page");
    render(
      <QueryClientProvider client={queryClient}>
        <NewPracticePage />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText("Synthetic Process Client"),
    ).toBeInTheDocument();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.click(screen.getByRole("button", { name: "Create Process" }));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("/clients/7?tab=process");
    });
    expect(mockRefetchClientProfile).toHaveBeenCalledTimes(1);
    expect(mockRefetchClientProfile.mock.invocationCallOrder[0]).toBeLessThan(
      mockPush.mock.invocationCallOrder[0],
    );
    expect(queryClient.getQueryData(queryKey)).toMatchObject({
      practices: [{ id: 71 }],
      stats: { practices_count: 1 },
    });
  });

  async function renderPage() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { default: NewPracticePage } = await import("./page");
    render(
      <QueryClientProvider client={queryClient}>
        <NewPracticePage />
      </QueryClientProvider>,
    );
    await screen.findByText("Synthetic Process Client");
  }

  it("sends the prefilled company with the new process", async () => {
    const user = userEvent.setup();
    mockSuggest.mockResolvedValue({
      autofill: true,
      suggested_company_id: 21,
      companies: [
        {
          id: 21,
          company_name: "PT Synthetic Satu",
          npwp_company: null,
          role: "director",
          is_director: true,
        },
      ],
    });
    await renderPage();
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "Invoice to company" }),
      ).toHaveValue("21"),
    );
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalled());
    expect(mockCreatePractice.mock.calls[0][0]).toMatchObject({
      client_id: 7,
      invoice_company_id: 21,
    });
  });

  it("sends nothing about a company when the field is left empty", async () => {
    const user = userEvent.setup();
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalled());
    const body = mockCreatePractice.mock.calls[0][0];
    expect(
      Object.keys(body).filter((k) => k.startsWith("invoice_company")),
    ).toEqual([]);
  });

  it("a 409 on a typed name shows the chooser and the pick is sent on the next Create", async () => {
    const user = userEvent.setup();
    mockCreatePractice.mockRejectedValueOnce(
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
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(
      await screen.findByLabelText("Company name"),
      "Synthetic Kembar",
    );
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    expect(
      await screen.findByTestId("invoice-company-ambiguity"),
    ).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "PT Synthetic Kembar" }),
    );
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(2));
    expect(mockCreatePractice.mock.calls[1][0]).toMatchObject({
      invoice_company_id: 8,
    });
    expect(mockCreatePractice.mock.calls[1][0]).not.toHaveProperty(
      "invoice_company_name",
    );
  });

  it.each([["no suggestion"], ["a failed suggestion"]])(
    "the first client's company is not sent after Change to another client (%s)",
    async (label) => {
      const user = userEvent.setup();
      mockSuggest.mockImplementation(async (clientId: number) => {
        if (clientId === 7) {
          return {
            autofill: true,
            suggested_company_id: 21,
            companies: [
              {
                id: 21,
                company_name: "PT Synthetic Satu",
                npwp_company: null,
                role: "director",
                is_director: true,
              },
            ],
          };
        }
        if (label === "a failed suggestion") throw new Error("down");
        return { autofill: true, suggested_company_id: null, companies: [] };
      });
      mockGetClients.mockResolvedValue([
        {
          id: 8,
          full_name: "Synthetic Other Client",
          email: "other@example.test",
        },
      ]);
      await renderPage();
      await waitFor(() =>
        expect(
          screen.getByRole("combobox", { name: "Invoice to company" }),
        ).toHaveValue("21"),
      );
      const selects = await screen.findAllByRole("combobox");
      await user.selectOptions(selects[0], "visa");
      await user.selectOptions(selects[1], "SYNTHETIC_VISA");
      await user.click(screen.getByRole("button", { name: "Change" }));
      await user.type(screen.getByLabelText("Search client"), "Other");
      await user.click(await screen.findByText("Synthetic Other Client"));
      await waitFor(() =>
        expect(mockSuggest).toHaveBeenCalledWith(8, "SYNTHETIC_VISA"),
      );
      await user.click(screen.getByRole("button", { name: "Create Process" }));
      await waitFor(() => expect(mockCreatePractice).toHaveBeenCalled());
      const body = mockCreatePractice.mock.calls[0][0];
      expect(body.client_id).toBe(8);
      expect(
        Object.keys(body).filter((k) => k.startsWith("invoice_company")),
      ).toEqual([]);
    },
  );

  it("an NPWP without a company name blocks Create instead of being dropped", async () => {
    const user = userEvent.setup();
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(await screen.findByLabelText("Company NPWP"), "01.234");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    expect(mockCreatePractice).not.toHaveBeenCalled();
    expect(mockToastError).toHaveBeenCalledWith(
      "Invoice company",
      expect.stringContaining("company name"),
    );
  });

  it("a suggestion that resolves during submission neither fills the field nor changes the payload", async () => {
    const user = userEvent.setup();
    let resolveSuggest: (v: unknown) => void = () => {};
    let pending = false;
    mockSuggest.mockImplementation((_id: number, code?: string) => {
      if (!code)
        return Promise.resolve({
          autofill: true,
          suggested_company_id: null,
          companies: [],
        });
      pending = true;
      return new Promise((r) => (resolveSuggest = r));
    });
    let resolveCreate: (v: unknown) => void = () => {};
    mockCreatePractice.mockReturnValueOnce(
      new Promise((r) => (resolveCreate = r)),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await waitFor(() => expect(pending).toBe(true));
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
    resolveSuggest({
      autofill: true,
      suggested_company_id: 21,
      companies: [
        {
          id: 21,
          company_name: "PT Synthetic Satu",
          npwp_company: null,
          role: "director",
          is_director: true,
        },
      ],
    });
    resolveCreate({ id: 71 });
    await waitFor(() => expect(mockPush).toHaveBeenCalled());
    const body = mockCreatePractice.mock.calls[0][0];
    expect(
      Object.keys(body).filter((k) => k.startsWith("invoice_company")),
    ).toEqual([]);
  });

  it("a candidate picked from the 409 chooser keeps its name in the selector", async () => {
    const user = userEvent.setup();
    mockCreatePractice.mockRejectedValueOnce(
      new ApiError("conflict", 409, {
        detail: {
          code: "invoice_company_ambiguous",
          candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
        },
      }),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(
      await screen.findByLabelText("Company name"),
      "Synthetic Kembar",
    );
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await user.click(
      await screen.findByRole("button", { name: "PT Synthetic Kembar" }),
    );
    const select = await screen.findByRole("combobox", {
      name: "Invoice to company",
    });
    expect(select).toHaveValue("8");
    expect(
      screen.getByRole("option", { name: "PT Synthetic Kembar" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Company name")).toBeNull();
  });

  it("client, category and service are locked from the click, preflight included", async () => {
    const user = userEvent.setup();
    let resolvePractices: (v: unknown) => void = () => {};
    mockGetClientPractices.mockReturnValueOnce(
      new Promise((r) => (resolvePractices = r)),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockGetClientPractices).toHaveBeenCalled());
    const during = screen.getAllByRole("combobox");
    expect(during[0]).toBeDisabled();
    expect(during[1]).toBeDisabled();
    expect(screen.getByRole("button", { name: "Change" })).toBeDisabled();
    resolvePractices([]);
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
  });

  it("category and service cannot change while the POST is in flight", async () => {
    const user = userEvent.setup();
    let resolveCreate: (v: unknown) => void = () => {};
    mockCreatePractice.mockReturnValueOnce(
      new Promise((r) => (resolveCreate = r)),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
    const during = screen.getAllByRole("combobox");
    expect(during[0]).toBeDisabled();
    expect(during[1]).toBeDisabled();
    resolveCreate({ id: 71 });
    await waitFor(() => expect(mockPush).toHaveBeenCalled());
  });

  it("a chooser already on screen is withdrawn when the service changes", async () => {
    const user = userEvent.setup();
    mockCreatePractice.mockRejectedValueOnce(
      new ApiError("conflict", 409, {
        detail: {
          code: "invoice_company_ambiguous",
          candidates: [{ id: 8, company_name: "PT Synthetic Kembar" }],
        },
      }),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(
      await screen.findByLabelText("Company name"),
      "Synthetic Kembar",
    );
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await screen.findByTestId("invoice-company-ambiguity");
    await user.selectOptions(screen.getAllByRole("combobox")[0], "");
    expect(screen.queryByTestId("invoice-company-ambiguity")).toBeNull();
  });

  it("a suggestion resolving during the preflight does not fill the field, and exactly one POST follows", async () => {
    const user = userEvent.setup();
    let resolveSuggest: (v: unknown) => void = () => {};
    let suggestAsked = false;
    mockSuggest.mockImplementation((_id: number, code?: string) => {
      if (!code)
        return Promise.resolve({
          autofill: true,
          suggested_company_id: null,
          companies: [],
        });
      suggestAsked = true;
      return new Promise((r) => (resolveSuggest = r));
    });
    let resolvePractices: (v: unknown) => void = () => {};
    mockGetClientPractices.mockReturnValueOnce(
      new Promise((r) => (resolvePractices = r)),
    );
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await waitFor(() => expect(suggestAsked).toBe(true));
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockGetClientPractices).toHaveBeenCalled());
    resolveSuggest({
      autofill: true,
      suggested_company_id: 21,
      companies: [
        {
          id: 21,
          company_name: "PT Synthetic Satu",
          npwp_company: null,
          role: "director",
          is_director: true,
        },
      ],
    });
    await screen.findByRole("combobox", { name: "Invoice to company" });
    expect(
      screen.getByRole("combobox", { name: "Invoice to company" }),
    ).toHaveValue("");
    resolvePractices([]);
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
    const body = mockCreatePractice.mock.calls[0][0];
    expect(
      Object.keys(body).filter((k) => k.startsWith("invoice_company")),
    ).toEqual([]);
  });

  it("a preselected client that loads late never replaces a client picked by hand", async () => {
    const user = userEvent.setup();
    let resolvePreselected: (v: unknown) => void = () => {};
    mockGetClient.mockReturnValueOnce(
      new Promise((r) => (resolvePreselected = r)),
    );
    mockGetClients.mockResolvedValue([
      {
        id: 8,
        full_name: "Synthetic Other Client",
        email: "other@example.test",
      },
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { default: NewPracticePage } = await import("./page");
    render(
      <QueryClientProvider client={queryClient}>
        <NewPracticePage />
      </QueryClientProvider>,
    );
    await user.type(await screen.findByLabelText("Search client"), "Other");
    await user.click(await screen.findByText("Synthetic Other Client"));
    resolvePreselected({
      id: 7,
      uuid: "synthetic-client-7",
      full_name: "Synthetic Process Client",
      email: "synthetic.process@example.test",
      status: "active",
      client_type: "individual",
      created_at: "2026-07-27T00:00:00Z",
      updated_at: "2026-07-27T00:00:00Z",
    });
    await waitFor(() => expect(mockGetClient).toHaveBeenCalled());
    await Promise.resolve();
    expect(screen.getByText("Synthetic Other Client")).toBeInTheDocument();
    expect(screen.queryByText("Synthetic Process Client")).toBeNull();
  });
});
