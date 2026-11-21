/**
 * "Done today" on the accounting page: Reconciliation tab only, one load on opening the tab,
 * one more load (with one more summary) after a statement import. Data is invented.
 */

import { render, screen, fireEvent, within } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import AccountingPage from "../page";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { importFixture, inboxItem, previewFixture } from "./fixtures";

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
  api.getMatchCandidates.mockImplementation(async (id) => ({
    txn_id: id,
    transfer_amount_idr: 500000,
    candidates: [],
    recorded_candidates: [],
  }));
  api.getDoneToday.mockResolvedValue({
    day: "2026-11-18",
    timezone: "Asia/Makassar",
    counts: { confirmed: 0, linked: 0, ignored: 0, total: 0 },
    items: [],
    truncated: false,
  });
});

describe("AccountingPage Done today", () => {
  it("the cashout view has no Done tab and opening it sends no new done load", async () => {
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    fireEvent.click(
      within(screen.getByTestId("cc-cashout-card")).getByRole("button", {
        name: "Open",
      }),
    );
    expect(await screen.findByText("No cashout rows yet.")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^done today/i })).toBeNull();
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
  });

  it("the page opens with Done today loaded once", async () => {
    render(<AccountingPage />);
    expect(
      await screen.findByRole("tab", { name: /^done today/i }),
    ).toHaveTextContent("0");
    await screen.findByText("CONTOH TRANSFER");
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
  });

  it("a statement import through the inbox card causes one more load and one more summary", async () => {
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockResolvedValue(importFixture());
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    await screen.findByRole("tab", { name: /^done today/i });
    const doneCalls = api.getDoneToday.mock.calls.length;
    const summaryCalls = api.getSummary.mock.calls.length;

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "contoh.csv", { type: "text/csv" })] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    await screen.findByText(/new transactions stored/);
    await vi.waitFor(() => {
      expect(api.getDoneToday.mock.calls.length).toBe(doneCalls + 1);
      expect(api.getSummary.mock.calls.length).toBe(summaryCalls + 1);
    });
  });
});
