/**
 * "Done today" on the accounting page: Reconciliation tab only, one load on opening the tab,
 * one more load (with one more summary) after a statement import. Data is invented.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
    undoConfirmation: vi.fn(),
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
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
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

  const confirmedToday = {
    day: "2026-11-18",
    timezone: "Asia/Makassar",
    counts: { confirmed: 1, linked: 0, ignored: 0, total: 1 },
    items: [
      {
        txn_id: 5,
        action: "confirmed" as const,
        done_at: "2026-11-18T10:00:00+08:00",
        txn_date: "2026-11-17",
        amount_idr: 4000000,
        description: "TRF CONTOH DUA",
        invoice_number: "INV-000000-00002",
        client_name: "PT CONTOH DUA",
      },
    ],
    truncated: false,
  };
  const toUndo = async () => {
    api.getDoneToday.mockResolvedValue(confirmedToday);
    render(<AccountingPage />);
    await screen.findByText("CONTOH TRANSFER");
    fireEvent.click(screen.getByTestId("cc-tab-done"));
    fireEvent.click(await screen.findByTestId("done-today-undo-5"));
    fireEvent.click(screen.getByTestId("done-today-undo-confirm-5"));
  };

  it("an undo posts that transfer once, then reloads the inbox and Done today", async () => {
    api.undoConfirmation.mockResolvedValue({
      bank_txn_id: 5,
      reconciled_status: "unmatched",
      reconciliation_log_id: 9,
    } as never);
    await toUndo();
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Confirmation undone"),
    );
    expect(api.undoConfirmation).toHaveBeenCalledTimes(1);
    expect(api.undoConfirmation).toHaveBeenCalledWith(5);
    await waitFor(() => expect(api.getDoneToday).toHaveBeenCalledTimes(2));
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2);
    for (const name of [
      "confirm",
      "linkRecorded",
      "ignoreTransaction",
      "reopenTransaction",
    ] as const)
      expect(api[name], name).not.toHaveBeenCalled();
  });

  it("a refused undo shows the reason under the entry and reloads nothing", async () => {
    api.undoConfirmation.mockRejectedValue(
      new Error(
        "bank transaction 5 was confirmed before today: it cannot be undone",
      ),
    );
    await toUndo();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "confirmed before today",
    );
    expect(toast.error).toHaveBeenCalledWith(
      "Undo failed",
      "bank transaction 5 was confirmed before today: it cannot be undone",
    );
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
  });
});
