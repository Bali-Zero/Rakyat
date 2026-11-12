import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { casesMetrics } from "@/lib/metrics/cases-metrics";
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
  mockSearch,
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
  mockSearch: vi.fn(),
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
      searchInvoiceCompanies: mockSearch,
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
    mockSearch.mockResolvedValue({ companies: [] });
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

  it("with no client chosen there is no suggestion request and Create cannot send anything", async () => {
    const user = userEvent.setup();
    const original = stableSearchParams.get.getMockImplementation();
    stableSearchParams.get.mockImplementation(() => null);
    try {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const { default: NewPracticePage } = await import("./page");
      render(
        <QueryClientProvider client={queryClient}>
          <NewPracticePage />
        </QueryClientProvider>,
      );
      const create = await screen.findByRole("button", {
        name: "Create Process",
      });
      expect(create).toBeDisabled();
      await user.click(create);
      expect(mockSuggest).not.toHaveBeenCalled();
      expect(mockCreatePractice).not.toHaveBeenCalled();
    } finally {
      if (original) stableSearchParams.get.mockImplementation(original);
    }
  });

  it("a company picked from the live search is sent by id and nothing is typed in", async () => {
    const user = userEvent.setup();
    mockSearch.mockResolvedValue({
      companies: [{ id: 31, company_name: "PT BAYU BALI NOL", linked: false }],
    });
    await renderPage();
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(await screen.findByLabelText("Company name"), "bayu");
    await user.click(
      await screen.findByRole("button", { name: "PT BAYU BALI NOL" }),
    );
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
    const body = mockCreatePractice.mock.calls[0][0];
    expect(body).toMatchObject({ invoice_company_id: 31 });
    expect(body).not.toHaveProperty("invoice_company_name");
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

  it("with a slow preselected client the visible client search is locked while creating", async () => {
    const user = userEvent.setup();
    let resolveCreate: (v: unknown) => void = () => {};
    mockGetClient.mockReturnValueOnce(new Promise(() => {}));
    mockCreatePractice.mockReturnValueOnce(
      new Promise((r) => (resolveCreate = r)),
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
    const search = await screen.findByLabelText("Search client");
    const selects = await screen.findAllByRole("combobox");
    await user.selectOptions(selects[0], "visa");
    await user.selectOptions(selects[1], "SYNTHETIC_VISA");
    await user.type(search, "Other");
    const result = await screen.findByText("Synthetic Other Client");
    await user.click(screen.getByRole("button", { name: "Create Process" }));
    await waitFor(() => expect(mockCreatePractice).toHaveBeenCalledTimes(1));
    expect(search).toBeDisabled();
    // The outside click closes the list; if it were still open it must be locked.
    const stillOpen = screen.queryByText("Synthetic Other Client");
    if (stillOpen) expect(stillOpen.closest("button")).toBeDisabled();
    expect(result).toBeDefined();
    resolveCreate({ id: 71 });
    await waitFor(() => expect(mockPush).toHaveBeenCalled());
  });

  describe("client search typeahead", () => {
    const found = (id: number, name: string) => ({
      id,
      full_name: name,
      email: `c${id}@example.test`,
    });

    async function renderSearch() {
      mockGetClient.mockReturnValueOnce(new Promise(() => {}));
      mockGetClients.mockReset();
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const { default: NewPracticePage } = await import("./page");
      render(
        <QueryClientProvider client={queryClient}>
          <NewPracticePage />
        </QueryClientProvider>,
      );
      return screen.findByLabelText("Search client");
    }

    it("an older response that arrives after a newer one is dropped", async () => {
      const user = userEvent.setup();
      let resolveOld: (v: unknown) => void = () => {};
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(
        () => new Promise((resolve) => (resolveOld = resolve)),
      );
      mockGetClients.mockResolvedValueOnce([found(2, "Synthetic Beta")]);
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      await user.type(input, "ri");
      await screen.findByText("Synthetic Beta");
      resolveOld([found(1, "Synthetic Alpha")]);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByText("Synthetic Alpha")).toBeNull();
      expect(screen.getByText("Synthetic Beta")).toBeInTheDocument();
    });

    it("clearing the text stops the spinner at once, not after the debounce", async () => {
      const user = userEvent.setup();
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(() => new Promise(() => {}));
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      expect(document.querySelector(".animate-spin")).not.toBeNull();
      await user.clear(input);
      expect(document.querySelector(".animate-spin")).toBeNull();
    });

    it("revert to an earlier text ignores the original request and shows the current one", async () => {
      const user = userEvent.setup();
      let resolveFirst: (v: unknown) => void = () => {};
      let resolveSecond: (v: unknown) => void = () => {};
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(
        () => new Promise((resolve) => (resolveFirst = resolve)),
      );
      mockGetClients.mockImplementationOnce(
        () => new Promise((resolve) => (resolveSecond = resolve)),
      );
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      await user.type(input, "b");
      await user.type(input, "{Backspace}");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
      resolveFirst([found(1, "Synthetic Original")]);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByText("Synthetic Original")).toBeNull();
      resolveSecond([found(2, "Synthetic Current")]);
      expect(await screen.findByText("Synthetic Current")).toBeInTheDocument();
    });

    it("closing the list (outside click or Escape) mid-request drops the response and a late failure; reopening searches again", async () => {
      const user = userEvent.setup();
      let resolveFirst: (v: unknown) => void = () => {};
      let rejectSecond: (e: Error) => void = () => {};
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(
        () => new Promise((resolve) => (resolveFirst = resolve)),
      );
      mockGetClients.mockImplementationOnce(
        () => new Promise((_, reject) => (rejectSecond = reject)),
      );
      mockGetClients.mockResolvedValueOnce([found(3, "Synthetic Reopened")]);
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      await user.click(document.body);
      resolveFirst([found(1, "Synthetic Closed")]);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByText("Synthetic Closed")).toBeNull();
      await new Promise((resolve) => setTimeout(resolve, 400));
      expect(mockGetClients).toHaveBeenCalledTimes(1);
      await user.click(input);
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
      await user.keyboard("{Escape}");
      rejectSecond(new Error("late failure"));
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(casesMetrics.trackError).not.toHaveBeenCalled();
      await user.click(document.body);
      await user.click(input);
      expect(await screen.findByText("Synthetic Reopened")).toBeInTheDocument();
    });

    it("a request that fails after a newer one started is not reported", async () => {
      const user = userEvent.setup();
      let rejectOld: (e: Error) => void = () => {};
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(
        () => new Promise((_, reject) => (rejectOld = reject)),
      );
      mockGetClients.mockResolvedValueOnce([found(2, "Synthetic Beta")]);
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      await user.type(input, "ri");
      await screen.findByText("Synthetic Beta");
      rejectOld(new Error("late failure"));
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(casesMetrics.trackError).not.toHaveBeenCalled();
      expect(screen.getByText("Synthetic Beta")).toBeInTheDocument();
    });

    it("clearing the text while a request is in flight leaves no list", async () => {
      const user = userEvent.setup();
      let resolveOld: (v: unknown) => void = () => {};
      const input = await renderSearch();
      mockGetClients.mockImplementationOnce(
        () => new Promise((resolve) => (resolveOld = resolve)),
      );
      await user.type(input, "ma");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(1));
      await user.clear(input);
      resolveOld([found(1, "Synthetic Alpha")]);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByText("Synthetic Alpha")).toBeNull();
    });

    it("a failing request leaves no list, not the previous text's list", async () => {
      const user = userEvent.setup();
      const input = await renderSearch();
      mockGetClients.mockResolvedValueOnce([found(1, "Synthetic Alpha")]);
      mockGetClients.mockRejectedValue(new Error("boom"));
      await user.type(input, "ma");
      await screen.findByText("Synthetic Alpha");
      await user.type(input, "b");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(mockToastError).toBeDefined());
      expect(screen.queryByText("Synthetic Alpha")).toBeNull();
    });

    it("edit and revert shows no list until a request for the current text succeeds", async () => {
      const user = userEvent.setup();
      const input = await renderSearch();
      mockGetClients.mockResolvedValueOnce([found(1, "Synthetic Alpha")]);
      mockGetClients.mockImplementation(() => new Promise(() => {}));
      await user.type(input, "ma");
      await screen.findByText("Synthetic Alpha");
      await user.type(input, "b");
      await user.type(input, "{Backspace}");
      await waitFor(() => expect(mockGetClients).toHaveBeenCalledTimes(2));
      expect(screen.queryByText("Synthetic Alpha")).toBeNull();
    });
  });
});
