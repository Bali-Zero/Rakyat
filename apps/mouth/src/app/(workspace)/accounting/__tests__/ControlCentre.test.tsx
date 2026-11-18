import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { ReconciliationInbox } from "../_components/ReconciliationInbox";
import {
  candidate,
  importFixture,
  inboxItem,
  previewFixture,
} from "./fixtures";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    getReconciliationInbox: vi.fn(),
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
const openCandidate = candidate({
  invoice_id: 11,
  practice_id: 21,
  client_name: "PT CONTOH SATU",
  invoice_number: "INV-000000-00001",
  invoice_issued_at: "2026-11-06T00:00:00Z",
  service_name: "Example service",
  reasons: ["same amount", "number in memo", "same client"],
  invoice_amount_idr: 12500000,
});
const recordedCandidate = candidate({
  invoice_id: 12,
  practice_id: 22,
  client_name: "PT CONTOH DUA",
  invoice_number: null,
  reasons: ["same amount"],
});
const rows = () => [
  inboxItem({
    txn_id: 1,
    txn_date: "2026-11-16",
    amount_idr: 12500000,
    description: "CONTOH TRANSFER ONE",
    light: "green",
    open_candidates: 1,
    suggestion: openCandidate,
  }),
  inboxItem({
    txn_id: 2,
    txn_date: "2026-11-15",
    description: "CONTOH TRANSFER TWO",
    light: "recorded",
    recorded_candidates: 1,
    suggestion: recordedCandidate,
  }),
  inboxItem({
    txn_id: 3,
    description: "CONTOH TRANSFER THREE",
    light: "yellow",
    open_candidates: 1,
  }),
  inboxItem({
    txn_id: 4,
    description: "CONTOH TRANSFER FOUR",
    light: "red",
  }),
  inboxItem({
    txn_id: 5,
    txn_date: "raw date",
    description: "",
    light: "other" as never,
  }),
];
const done = {
  day: "2026-11-18",
  timezone: "Asia/Makassar",
  counts: { confirmed: 2, linked: 1, ignored: 1, total: 4 },
  items: [],
  truncated: false,
};
const bankRow = {
  id: 90,
  txn_date: "2026-11-16",
  description: "BANK ROW",
  amount_idr: 1,
  direction: "credit",
  reconciled_status: "unmatched",
} as never;
const emptyCandidates = (txnId: number) => ({
  txn_id: txnId,
  transfer_amount_idr: 500000,
  candidates: [],
  recorded_candidates: [],
});
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getReconciliationInbox.mockResolvedValue(rows());
  api.getBankTransactions.mockResolvedValue([bankRow]);
  api.getDoneToday.mockResolvedValue(done);
  api.getMatchCandidates.mockImplementation(async (id) => {
    if (id === 1)
      return {
        ...emptyCandidates(id),
        candidates: [openCandidate],
        transfer_amount_idr: 12500000,
      };
    if (id === 2)
      return {
        ...emptyCandidates(id),
        recorded_candidates: [recordedCandidate],
      };
    if (id === 3)
      return { ...emptyCandidates(id), candidates: [openCandidate] };
    return emptyCandidates(id);
  });
});

const renderCentre = (onOpenCashout = vi.fn(), onConfirmed = vi.fn()) => {
  render(
    <ReconciliationInbox
      onConfirmed={onConfirmed}
      doneVersion={0}
      onOpenCashout={onOpenCashout}
    />,
  );
  return { onOpenCashout, onConfirmed };
};
const ready = async () => screen.findByText("CONTOH TRANSFER ONE");
const tab = (key: string) => screen.getByTestId(`cc-tab-${key}`);
const opened = () => screen.getByTestId("cc-opened");

describe("Accounting control centre", () => {
  it("shows counts for four lights and maps an unknown light to To choose", async () => {
    renderCentre();
    await ready();
    expect(screen.getByTestId("cc-count-confirm")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-link")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("2");
    expect(screen.getByTestId("cc-count-nomatch")).toHaveTextContent("1");
  });

  it("selects the first non-empty tab and applies its situation classes", async () => {
    renderCentre();
    await ready();
    expect(tab("confirm")).toHaveAttribute("aria-selected", "true");
    expect(tab("confirm")).toHaveClass("cc-s-confirm", "bg-[var(--cc-solid)]");
    expect(screen.getByTestId("cc-count-confirm")).toHaveClass(
      "bg-[var(--cc-on)]",
    );
    expect(opened()).toHaveClass("cc-s-confirm");
  });

  it("disables a zero tab with a tick and no badge", async () => {
    api.getReconciliationInbox.mockResolvedValue([rows()[0]]);
    renderCentre();
    await ready();
    expect(tab("link")).toBeDisabled();
    expect(tab("link").querySelector("svg")).not.toBeNull();
    expect(screen.queryByTestId("cc-count-link")).toBeNull();
  });

  it("switches tabs, lists only their rows and opens the first row", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("link"));
    expect(await screen.findByText("CONTOH TRANSFER TWO")).toBeInTheDocument();
    expect(screen.queryByText("CONTOH TRANSFER ONE")).toBeNull();
    expect(screen.getByTestId("inbox-row-2")).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("keeps Money out inert", async () => {
    renderCentre();
    await ready();
    const moneyOut = screen.getByTestId("cc-money-out");
    expect(moneyOut.tagName).toBe("SPAN");
    expect(moneyOut).toHaveAttribute("aria-disabled", "true");
  });

  it("shows the Done tab count and the done view", async () => {
    renderCentre();
    await ready();
    expect(screen.getByTestId("cc-count-done")).toHaveTextContent("4");
    fireEvent.click(tab("done"));
    expect(screen.getByTestId("cc-done")).toHaveTextContent("Done today");
    expect(screen.getByText("Linked and ignored")).toBeInTheDocument();
  });

  it("removes Done today when the route is unavailable", async () => {
    api.getDoneToday.mockRejectedValue(
      Object.assign(new Error("not found"), { statusCode: 404 }),
    );
    renderCentre();
    await ready();
    await waitFor(() => expect(screen.queryByTestId("cc-tab-done")).toBeNull());
  });

  it("renders date blocks, empty memos and raw dates", async () => {
    renderCentre();
    await ready();
    expect(screen.getByTestId("inbox-row-1")).toHaveTextContent("16Nov");
    fireEvent.click(tab("choose"));
    expect(screen.getByTestId("inbox-row-5")).toHaveTextContent("raw date");
    expect(screen.getByTestId("inbox-row-5")).toHaveTextContent("(no memo)");
  });

  it("marks only the opened row", async () => {
    renderCentre();
    await ready();
    const row = screen.getByTestId("inbox-row-1");
    expect(row).toHaveAttribute("aria-current", "true");
    expect(row).toHaveClass("bg-[var(--cc-sel)]");
    expect(within(row).getByTestId("cc-marker")).toBeInTheDocument();
  });

  it("loads the automatically opened transfer and does not reload on a re-click", async () => {
    renderCentre();
    await ready();
    await waitFor(() => expect(api.getMatchCandidates).toHaveBeenCalledWith(1));
    const before = api.getMatchCandidates.mock.calls.length;
    fireEvent.click(screen.getByTestId("inbox-row-1"));
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(before);
  });

  it("shows the band and pair view with every reason", async () => {
    renderCentre();
    await ready();
    expect(
      await screen.findByText("One invoice matches this transfer"),
    ).toBeInTheDocument();
    expect(opened()).toHaveTextContent("From the bank");
    expect(opened()).toHaveTextContent("Our invoice");
    expect(opened()).toHaveTextContent(
      "same amount · number in memo · same client",
    );
  });

  it("opens the table without a request when Choose another is clicked", async () => {
    renderCentre();
    await ready();
    await screen.findByText("One invoice matches this transfer");
    const calls = api.getMatchCandidates.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Choose another" }));
    expect(screen.getByTestId("cc-cand-11")).toBeInTheDocument();
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(calls);
  });

  it("shows every candidate field, reason and the missing-number fallback", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("link"));
    await screen.findByText("This payment looks already recorded");
    fireEvent.click(screen.getByRole("button", { name: "Choose another" }));
    const row = screen.getByTestId("cc-cand-12");
    expect(row).toHaveTextContent("No invoice number");
    expect(row).toHaveTextContent("PT CONTOH DUA");
    expect(row).toHaveTextContent("same amount");
  });

  it("preselects nothing on To choose until a radio is chosen", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    const primary = screen.getByRole("button", { name: "Confirm payment" });
    expect(primary).toBeDisabled();
    fireEvent.click(
      within(screen.getByTestId("cc-cand-11")).getByRole("radio"),
    );
    expect(primary).toBeEnabled();
  });

  it("switches the primary label and footer when a recorded candidate is picked", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("link"));
    await screen.findByText("This payment looks already recorded");
    expect(
      screen.getByRole("button", { name: "Link, no money moved" }),
    ).toBeInTheDocument();
    expect(opened()).toHaveTextContent("Linking moves no money");
  });

  it("renders the nothing view for a No match transfer", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("nomatch"));
    expect(await screen.findByText("Nothing found")).toBeInTheDocument();
    expect(opened()).toHaveTextContent("≠");
  });

  it("wraps Skip for now and disables it for a single row", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    expect(screen.getByRole("button", { name: "Skip for now" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(screen.getByTestId("inbox-row-5")).toHaveAttribute(
      "aria-current",
      "true",
    );
    fireEvent.click(tab("nomatch"));
    expect(
      await screen.findByRole("button", { name: "Skip for now" }),
    ).toBeDisabled();
  });

  it("renders Rp separately with the pinned amount classes", async () => {
    renderCentre();
    await ready();
    const amount = within(screen.getByTestId("inbox-row-1")).getByText("Rp");
    expect(amount).toHaveClass("mr-[0.28em]", "text-[0.72em]", "font-medium");
    expect(amount.parentElement).toHaveTextContent("Rp12.500.000");
  });

  it("shows an ok bank date and sends the pinned request", async () => {
    renderCentre();
    await ready();
    expect(screen.getByTestId("cc-bank-date")).toHaveTextContent(
      "Bank data through Mon 16 Nov",
    );
    expect(api.getBankTransactions).toHaveBeenCalledWith({ limit: 1 });
  });

  it("shows empty and failed bank-date states", async () => {
    api.getBankTransactions.mockResolvedValueOnce([]);
    const first = renderCentre();
    await ready();
    expect(screen.getByText("No bank data yet")).toBeInTheDocument();
    first.onOpenCashout.mockClear();
  });

  it("hides the bank line while its request is loading", async () => {
    api.getBankTransactions.mockReturnValue(new Promise(() => {}));
    renderCentre();
    await ready();
    expect(screen.queryByTestId("cc-bank-date")).toBeNull();
  });

  it("clicks the hidden input from Upload statement", async () => {
    renderCentre();
    await ready();
    const input = screen.getByTestId("bank-statement-input");
    const click = vi.spyOn(input, "click");
    fireEvent.click(screen.getByRole("button", { name: "Upload statement" }));
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("reloads the inbox and bank date after an import", async () => {
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockResolvedValue(importFixture());
    renderCentre();
    await ready();
    const input = screen.getByTestId("bank-statement-input");
    fireEvent.change(input, {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    await waitFor(() =>
      expect(api.getBankTransactions).toHaveBeenCalledTimes(2),
    );
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2);
  });

  it("renders loading and failed inbox cards", async () => {
    const load = deferred<ReturnType<typeof rows>>();
    api.getReconciliationInbox.mockReturnValueOnce(load.promise);
    const view = renderCentre();
    expect(screen.getByText("Loading inbox…")).toBeInTheDocument();
    view.onOpenCashout.mockClear();
    await act(async () => load.reject(new Error("failed")));
    expect(screen.getByText("Could not load the inbox.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });

  it("renders the calm view when work is empty and bank data exists", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    renderCentre();
    expect(await screen.findByTestId("cc-calm")).toHaveTextContent(
      "All done for today",
    );
    expect(screen.getByTestId("cc-calm")).toHaveTextContent(
      "4 transfers handled",
    );
  });

  it("renders day one without tabs or header trigger and its CTA clicks the input", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getBankTransactions.mockResolvedValue([]);
    renderCentre();
    const dayone = await screen.findByTestId("cc-dayone");
    expect(dayone).toHaveTextContent("Upload this morning’s bank statement");
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Upload statement" }),
    ).toBeNull();
    const input = screen.getByTestId("bank-statement-input");
    const click = vi.spyOn(input, "click");
    fireEvent.click(
      screen.getByRole("button", { name: "Upload CIMB statement" }),
    );
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("shows a candidate failure and retries the same transfer", async () => {
    api.getMatchCandidates.mockRejectedValueOnce(new Error("rank failed"));
    renderCentre();
    await ready();
    expect(
      await screen.findByText("Could not load the invoices for this transfer."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenCalledTimes(2),
    );
    expect(api.getMatchCandidates).toHaveBeenLastCalledWith(1);
  });

  it("opens the cash-out view from the bottom card without an amount", async () => {
    const onOpenCashout = vi.fn();
    renderCentre(onOpenCashout);
    await ready();
    const card = screen.getByTestId("cc-cashout-card");
    expect(card).toHaveTextContent("Cash-out this week");
    expect(card).not.toHaveTextContent("Rp");
    fireEvent.click(within(card).getByRole("button", { name: "Open" }));
    expect(onOpenCashout).toHaveBeenCalledTimes(1);
  });

  it("sends only the four read requests on first render", async () => {
    renderCentre();
    await ready();
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenCalledTimes(1),
    );
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
    expect(api.getBankTransactions).toHaveBeenCalledTimes(1);
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    expect(api.confirm).not.toHaveBeenCalled();
  });
});
