/**
 * Accounting page smoke: both tabs render, the cashout import control is intact,
 * and the inbox carries the bank statement card. Data is invented.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import AccountingPage from "../page";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
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
  vi.clearAllMocks();
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
});

describe("AccountingPage", () => {
  it("U6: cashout tab, import-cashout control and inbox tab still render", async () => {
    render(<AccountingPage />);
    expect(await screen.findByText("No cashout rows yet.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /import cashout pdf/i }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /reconciliation/i }));
    expect(await screen.findByText("CONTOH TRANSFER")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/upload bank statement \(csv\)/i),
    ).toBeInTheDocument();
    expect(api.importBankStatement).not.toHaveBeenCalled();
  });

  it("U6: the cashout import still calls importCashout with the chosen file", async () => {
    api.importCashout.mockResolvedValue({
      source_filename: "contoh.pdf",
      imported: 1,
      weeks: [],
      replaced: 0,
    });
    const { container } = render(<AccountingPage />);
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
    fireEvent.click(
      await screen.findByRole("button", { name: /reconciliation/i }),
    );
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
});
