import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  accountingApi,
  type AccountingCompany,
} from "@/lib/api/workspace/accounting.api";
import { companyStatus } from "../_components/CompanyStrip";
import { ReconciliationInbox } from "../_components/ReconciliationInbox";
import { candidate, inboxItem } from "./fixtures";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    getReconciliationInbox: vi.fn(),
    getCompanies: vi.fn(),
    getMatchCandidates: vi.fn(),
    getBankTransactions: vi.fn(),
    getDoneToday: vi.fn(),
    linkRecorded: vi.fn(),
    ignoreTransaction: vi.fn(),
    reopenTransaction: vi.fn(),
    confirm: vi.fn(),
    previewBankStatement: vi.fn(),
    importBankStatement: vi.fn(),
  },
}));

const api = vi.mocked(accountingApi);
const company = (over: Partial<AccountingCompany>): AccountingCompany => ({
  key: "co_satu",
  company_name: "PT CONTOH SATU",
  bank_name: "BANK SATU",
  account_number: "000111",
  data_through: null,
  statements: 0,
  to_reconcile: 0,
  ...over,
});
const COMPANIES = [
  company({
    key: "co_satu",
    data_through: "2026-01-17",
    statements: 2,
    to_reconcile: 2,
  }),
  company({
    key: "co_dua",
    company_name: "PT CONTOH DUA",
    bank_name: "BANK DUA",
    account_number: "000-222",
    data_through: "2026-01-12",
    statements: 1,
    to_reconcile: 1,
  }),
  company({
    key: "co_tiga",
    company_name: "CV CONTOH TIGA",
    bank_name: "BANK TIGA",
    account_number: "000333",
  }),
];
const ITEMS = [
  inboxItem({
    txn_id: 1,
    light: "yellow",
    description: "SATU A",
    company_key: "co_satu",
  }),
  inboxItem({
    txn_id: 2,
    light: "red",
    description: "SATU B",
    company_key: "co_satu",
  }),
  inboxItem({
    txn_id: 3,
    light: "yellow",
    description: "DUA A",
    company_key: "co_dua",
  }),
];

beforeEach(() => {
  vi.resetAllMocks();
  api.getReconciliationInbox.mockResolvedValue(ITEMS);
  api.getCompanies.mockResolvedValue(COMPANIES);
  api.getMatchCandidates.mockImplementation(async (id) => ({
    txn_id: id,
    transfer_amount_idr: 500000,
    candidates: [candidate({ invoice_id: 11, practice_id: 21 })],
    recorded_candidates: [],
  }));
  api.getBankTransactions.mockResolvedValue([]);
  api.getDoneToday.mockResolvedValue({
    day: "2026-01-17",
    timezone: "Asia/Makassar",
    counts: { confirmed: 0, linked: 0, ignored: 0, total: 0 },
    items: [],
    truncated: false,
  });
  api.ignoreTransaction.mockResolvedValue({
    bank_txn_id: 3,
    reconciled_status: "ignored",
    reconciliation_log_id: 1,
  });
});

const renderInbox = () =>
  render(
    <ReconciliationInbox
      onConfirmed={vi.fn()}
      doneVersion={0}
      onOpenCashout={vi.fn()}
    />,
  );
const card = (key: string) => screen.getByTestId(`cc-company-${key}`);

describe("the companies strip", () => {
  it("asks for the whole waiting list, not the newest page", async () => {
    renderInbox();
    await screen.findByTestId("cc-companies");
    expect(api.getReconciliationInbox).toHaveBeenCalledWith(1000);
  });

  it("lists every company with bank, account and how far its data reaches", async () => {
    renderInbox();
    await screen.findByTestId("cc-companies");
    expect(card("all")).toHaveTextContent("All companies");
    expect(card("all")).toHaveTextContent("3 to reconcile");
    expect(card("co_satu")).toHaveTextContent("PT CONTOH SATU");
    expect(card("co_satu")).toHaveTextContent("BANK SATU · 000111");
    expect(card("co_satu")).toHaveTextContent("2 to reconcile");
    expect(card("co_dua")).toHaveTextContent("BANK DUA · 000-222");
    expect(card("co_tiga")).toHaveTextContent("No statement yet");
    expect(card("all")).toHaveAttribute("aria-checked", "true");
  });

  it("shows only the chosen company's transfers, and all again on a second click", async () => {
    renderInbox();
    await screen.findByText("SATU A");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("2");
    fireEvent.click(card("co_dua"));
    expect(card("co_dua")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("1");
    expect(screen.getByText("DUA A")).toBeInTheDocument();
    expect(screen.queryByText("SATU A")).not.toBeInTheDocument();
    expect(screen.getByTestId("cc-tab-nomatch")).toBeDisabled();
    expect(screen.getByTestId("cc-bank-date")).toHaveTextContent(
      "PT CONTOH DUA: bank data through",
    );
    fireEvent.click(card("co_dua"));
    expect(card("all")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("2");
  });

  it("a company with no statement shows nothing to work on and says so", async () => {
    renderInbox();
    await screen.findByText("SATU A");
    fireEvent.click(card("co_tiga"));
    expect(screen.queryByTestId("cc-work")).not.toBeInTheDocument();
    expect(screen.getByTestId("cc-bank-date")).toHaveTextContent(
      "CV CONTOH TIGA: no statement yet",
    );
  });

  it("reloads the companies after an action, so their counts follow", async () => {
    renderInbox();
    await screen.findByText("SATU A");
    fireEvent.click(card("co_dua"));
    const before = api.getCompanies.mock.calls.length;
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "not a client payment" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() =>
      expect(api.ignoreTransaction).toHaveBeenCalledWith({
        bank_txn_id: 3,
        reason: "not a client payment",
      }),
    );
    await waitFor(() =>
      expect(api.getCompanies.mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("says so when the companies cannot be loaded, and the transfers still show", async () => {
    api.getCompanies.mockRejectedValue(new Error("down"));
    renderInbox();
    expect(
      await screen.findByTestId("cc-companies-failed"),
    ).toBeInTheDocument();
    expect((await screen.findAllByText("SATU A")).length).toBeGreaterThan(0);
  });
});

describe("the companies strip, corrections of gate round 1", () => {
  it("choosing the other accounts shows their transfers", async () => {
    api.getCompanies.mockResolvedValue([
      ...COMPANIES,
      company({
        key: "other",
        company_name: "Other accounts",
        bank_name: null,
        account_number: null,
        data_through: "2026-01-10",
        statements: 1,
        to_reconcile: 1,
      }),
    ]);
    api.getReconciliationInbox.mockResolvedValue([
      ...ITEMS,
      inboxItem({
        txn_id: 9,
        light: "yellow",
        description: "LAIN A",
        company_key: "other",
      }),
    ]);
    renderInbox();
    await screen.findAllByText("SATU A");
    fireEvent.click(card("other"));
    expect(card("other")).toHaveAttribute("aria-checked", "true");
    expect((await screen.findAllByText("LAIN A")).length).toBeGreaterThan(0);
    expect(screen.getByTestId("cc-work")).toBeInTheDocument();
    expect(screen.queryByText("SATU A")).not.toBeInTheDocument();
  });

  it("the company cannot change while the confirm dialog is open", async () => {
    const pick = candidate({ invoice_id: 11, practice_id: 21 });
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 1,
        light: "green",
        description: "SATU HIJAU",
        company_key: "co_satu",
        open_candidates: 1,
        suggestion: pick,
      }),
      ...ITEMS.slice(2),
    ]);
    renderInbox();
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    await screen.findByText("Confirm payment match");
    for (const key of ["all", "co_satu", "co_dua", "co_tiga"])
      expect(card(key)).toBeDisabled();
    fireEvent.click(card("co_dua"));
    expect(card("all")).toHaveAttribute("aria-checked", "true");
  });
});

describe("companyStatus", () => {
  it("names the date and what waits, or that nothing is imported", () => {
    expect(companyStatus(COMPANIES[0])).toMatch(
      /^Data through .+ · 2 to reconcile$/,
    );
    expect(companyStatus({ ...COMPANIES[0], to_reconcile: 0 })).toMatch(
      /· all reconciled$/,
    );
    expect(companyStatus(COMPANIES[2])).toBe("No statement yet");
  });
});
