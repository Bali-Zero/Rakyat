/**
 * Accounting page smoke: both tabs render, the cashout import control is intact,
 * and the inbox carries the bank statement card. Data is invented.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import AccountingPage from "../page";
import {
  accountingApi,
  type CashoutRow,
} from "@/lib/api/workspace/accounting.api";
import { importFixture, inboxItem, previewFixture } from "./fixtures";

// Stable identities: the page's load callbacks depend on them.
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    getSummary: vi.fn(),
    getCashout: vi.fn(),
    getBankTransactions: vi.fn(),
    getReconciliationInbox: vi.fn(),
    getDoneToday: vi.fn(),
    linkRecorded: vi.fn(),
    ignoreTransaction: vi.fn(),
    reopenTransaction: vi.fn(),
    getMatchCandidates: vi.fn(),
    confirm: vi.fn(),
    exportSheet: vi.fn(),
    importCashout: vi.fn(),
    previewBankStatement: vi.fn(),
    importBankStatement: vi.fn(),
    getOutgoings: vi.fn(),
    getCompanies: vi.fn(),
  },
}));

const api = vi.mocked(accountingApi);

beforeEach(() => {
  vi.resetAllMocks();
  api.getSummary.mockResolvedValue({
    income_idr: 0,
    outgoing_idr: 0,
    net_idr: 0,
    margin_total_idr: 0,
    by_type: [],
  } as never);
  api.getCashout.mockResolvedValue([]);
  api.getReconciliationInbox.mockResolvedValue([
    inboxItem({ txn_id: 7, description: "CONTOH TRANSFER" }),
  ]);
  api.getBankTransactions.mockResolvedValue([
    { id: 1, txn_date: "2026-11-16" } as never,
  ]);
  api.getDoneToday.mockResolvedValue({
    day: "2026-11-18",
    timezone: "Asia/Makassar",
    counts: { confirmed: 0, linked: 0, ignored: 0, total: 0 },
    items: [],
    truncated: false,
  });
  api.getCompanies.mockResolvedValue([
    { key: "test", company_name: "Test company", bank_name: null, account_number: null, data_through: null, statements: 0, to_reconcile: 0 },
  ]);
  api.getMatchCandidates.mockImplementation(async (id) => ({
    txn_id: id,
    transfer_amount_idr: 500000,
    candidates: [],
    recorded_candidates: [],
  }));
});

describe("AccountingPage", () => {
  it("opens on November and restores full history only when selected", async () => {
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    expect(api.getReconciliationInbox).toHaveBeenCalledWith(1000, "2026-11-01");
    expect(api.getDoneToday).toHaveBeenCalledWith("2026-11-01");
    expect(api.getCashout).toHaveBeenCalledWith({
      limit: 200,
      month: "2026-11-01",
    });
    expect(api.getSummary).toHaveBeenCalledWith({
      periodStart: "2026-11-01",
      periodEnd: "2026-11-30",
    });
    fireEvent.change(screen.getByLabelText("Period"), {
      target: { value: "archive" },
    });
    await screen.findByText("CONTOH TRANSFER");
    expect(api.getReconciliationInbox).toHaveBeenLastCalledWith(
      1000,
      undefined,
    );
    expect(api.getSummary).toHaveBeenLastCalledWith(undefined);
  });

  it("86: the control-centre container carries the P2 padding classes", async () => {
    const { container } = render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    expect(container.firstElementChild).toHaveClass("px-4", "pb-2", "pt-1.5");
    expect(container.firstElementChild).not.toHaveClass("p-6");
  });

  it("U6: opens on the control centre and can enter and leave cashout", async () => {
    render(<AccountingPage />);
    expect(await screen.findByText("CONTOH TRANSFER")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/upload bank statement \(csv or pdf\)/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Income (in)")).toBeNull();
    expect(screen.queryByText("Weekly Cashout")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByText("No cashout rows yet.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /import cashout pdf/i }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Back to reconciliation" }),
    );
    expect(await screen.findByText("CONTOH TRANSFER")).toBeInTheDocument();
    expect(api.importBankStatement).not.toHaveBeenCalled();
  });

  it("the cashout Process column names the case's service before the raw type", async () => {
    const row = (id: number, extra: Partial<CashoutRow>): CashoutRow => ({
      id,
      movement_date: "2026-06-25",
      week_label: "2026-W26",
      direction: "in",
      type: "invoice_payment",
      amount_idr: 1000000,
      pnbp_idr: 0,
      urgent_idr: 0,
      rptka_imta_idr: 0,
      margin_idr: 0,
      final_price_idr: 1000000,
      original_currency: null,
      original_amount: null,
      fx_rate: null,
      fx_date: null,
      pph_withheld_idr: 0,
      bank_fee_idr: 0,
      linked_practice_id: null,
      linked_invoice_id: null,
      category: null,
      description: null,
      counterparty: null,
      payment_method: null,
      recorded_by: null,
      recorded_at: null,
      reversed_by_id: null,
      client_name: `CONTOH KLIEN ${id}`,
      ...extra,
    });
    api.getCashout.mockResolvedValue([
      row(1, { service_name: "Contoh Layanan" }),
      row(2, { category: "Contoh Kategori", service_name: "Contoh Layanan" }),
      row(3, {}),
    ]);
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    const cells = async (name: string) =>
      Array.from(
        (await screen.findByText(name)).closest("tr")?.querySelectorAll("td") ??
          [],
      ).map((td) => td.textContent);
    expect((await cells("CONTOH KLIEN 1"))[2]).toBe("Contoh Layanan");
    expect((await cells("CONTOH KLIEN 2"))[2]).toBe("Contoh Kategori");
    expect((await cells("CONTOH KLIEN 3"))[2]).toBe("invoice_payment");
  });

  it("U6: the cashout import still calls importCashout with the chosen file", async () => {
    api.importCashout.mockResolvedValue({
      source_filename: "contoh.pdf",
      imported: 1,
      weeks: [],
      replaced: 0,
    });
    const { container } = render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByText("No cashout rows yet.");
    const pdf = new File(["x"], "contoh.pdf", { type: "application/pdf" });
    const el = container.querySelector(
      'input[accept="application/pdf,.pdf"]',
    ) as HTMLInputElement;
    fireEvent.change(el, { target: { files: [pdf] } });
    await vi.waitFor(() => expect(api.importCashout).toHaveBeenCalledWith(pdf));
  });

  it("an import from the inbox card reloads the inbox list and the page summary", async () => {
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockResolvedValue(importFixture());
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    const inboxCalls = api.getReconciliationInbox.mock.calls.length;
    const summaryCalls = api.getSummary.mock.calls.length;

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "contoh.csv", { type: "text/csv" })] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    await screen.findByText(/new transactions stored/);
    await vi.waitFor(() => {
      expect(api.getReconciliationInbox.mock.calls.length).toBe(inboxCalls + 1);
      expect(api.getSummary.mock.calls.length).toBe(summaryCalls + 1);
    });
    // the result line survives the list reload
    expect(screen.getByText(/new transactions stored/)).toBeInTheDocument();
  });

  it("enters the outgoings view from Money out and comes back", async () => {
    api.getOutgoings.mockResolvedValue({
      items: [],
      by_kind: [],
      n: 0,
      total_idr: 0,
      n_justified: 0,
      justified_total_idr: 0,
    });
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    expect(api.getOutgoings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("cc-money-out"));
    expect(
      await screen.findByRole("heading", { name: "Outgoings" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Nothing to justify.")).toBeInTheDocument();
    expect(api.getOutgoings).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("back-to-centre"));
    expect(await screen.findByText("CONTOH TRANSFER")).toBeInTheDocument();
    expect(screen.queryByTestId("outgoings")).toBeNull();
  });
});
