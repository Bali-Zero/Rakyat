/**
 * Reconciliation inbox with lights and the three no-money actions.
 * All names and amounts are invented.
 */

import {
  act,
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { ReconciliationInbox } from "../_components/ReconciliationInbox";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
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
    linkRecorded: vi.fn(),
    ignoreTransaction: vi.fn(),
    reopenTransaction: vi.fn(),
    confirm: vi.fn(),
    previewBankStatement: vi.fn(),
    importBankStatement: vi.fn(),
  },
}));

const api = vi.mocked(accountingApi);
const row = (id: number) => screen.getByTestId(`inbox-row-${id}`);

const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const ack = {
  bank_txn_id: 1,
  reconciled_status: "manual",
  reconciliation_log_id: 1,
};

const four = () => [
  inboxItem({ txn_id: 1, light: "green", description: "ROW GREEN" }),
  inboxItem({ txn_id: 2, light: "recorded", description: "ROW RECORDED" }),
  inboxItem({ txn_id: 3, light: "yellow", description: "ROW YELLOW" }),
  inboxItem({ txn_id: 4, light: "red", description: "ROW RED" }),
];

beforeEach(() => {
  vi.clearAllMocks();
  api.getReconciliationInbox.mockResolvedValue(four());
  api.getBankTransactions.mockResolvedValue([]);
  api.getMatchCandidates.mockResolvedValue({
    txn_id: 2,
    transfer_amount_idr: 500000,
    candidates: [],
    recorded_candidates: [candidate({ invoice_id: 12, practice_id: 22 })],
  });
});

const renderInbox = async (onConfirmed = vi.fn()) => {
  render(<ReconciliationInbox onConfirmed={onConfirmed} />);
  await screen.findByText("ROW GREEN");
  return onConfirmed;
};

const expandRecorded = async () => {
  fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
  return screen.findByRole("button", { name: "Link, no money moved" });
};

describe("lights and filters", () => {
  it("V1: each chip shows the server's light; an unknown value shows To check", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      ...four(),
      inboxItem({
        txn_id: 5,
        description: "ROW ODD",
        light: "purple" as never,
      }),
    ]);
    await renderInbox();
    expect(within(row(1)).getByText("Match found")).toBeInTheDocument();
    expect(within(row(2)).getByText("Already recorded")).toBeInTheDocument();
    expect(within(row(3)).getByText("To check")).toBeInTheDocument();
    expect(within(row(4)).getByText("No invoice")).toBeInTheDocument();
    expect(within(row(5)).getByText("To check")).toBeInTheDocument();
  });

  it("V12: the expand button carries aria-expanded, true only for the open row", async () => {
    await renderInbox();
    const btn = (id: number) =>
      within(row(id))
        .getByText(`ROW ${["", "GREEN", "RECORDED"][id]}`)
        .closest("button") as HTMLElement;
    expect(btn(1)).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(btn(2));
    expect(btn(2)).toHaveAttribute("aria-expanded", "true");
    expect(btn(1)).toHaveAttribute("aria-expanded", "false");
  });

  it("filter chips carry counts and filter client-side", async () => {
    await renderInbox();
    expect(screen.getByRole("button", { name: "All (4)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "No invoice (1)" }));
    expect(screen.getByText("ROW RED")).toBeInTheDocument();
    expect(screen.queryByText("ROW GREEN")).toBeNull();
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
  });

  it("V8: rendering sends only the inbox GET, whatever the lights", async () => {
    await renderInbox();
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
    expect(api.getMatchCandidates).not.toHaveBeenCalled();
    expect(api.getBankTransactions).not.toHaveBeenCalled();
    for (const m of [
      api.linkRecorded,
      api.ignoreTransaction,
      api.reopenTransaction,
      api.confirm,
      api.previewBankStatement,
      api.importBankStatement,
    ]) {
      expect(m).not.toHaveBeenCalled();
    }
  });

  it("a recorded row shows the note and the Suggested mark in Already paid", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 2,
        light: "recorded",
        description: "ROW RECORDED",
        suggestion: candidate({ invoice_id: 12, practice_id: 22 }),
      }),
    ]);
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW RECORDED"));
    await screen.findByRole("button", { name: "Link, no money moved" });
    expect(
      screen.getByText(
        "This payment looks already recorded. Link it instead of confirming it again.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Already paid")).toBeInTheDocument();
    expect(screen.getByText("Suggested")).toBeInTheDocument();
  });
});

describe("link", () => {
  it("V2: link is sent only on click, with the row's txn_id and the candidate's ids", async () => {
    api.linkRecorded.mockResolvedValue(ack);
    const onConfirmed = await renderInbox();
    const btn = await expandRecorded();
    expect(api.linkRecorded).not.toHaveBeenCalled();
    fireEvent.click(btn);
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 2,
      practice_id: 22,
      invoice_id: 12,
    });
  });

  it("V6: after a link the inbox reloads and onConfirmed is called", async () => {
    api.linkRecorded.mockResolvedValue(ack);
    const onConfirmed = await renderInbox();
    fireEvent.click(await expandRecorded());
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("V6: a link error shows the server's text and leaves the row", async () => {
    api.linkRecorded.mockRejectedValue(new Error("fixed server text"));
    const onConfirmed = await renderInbox();
    fireEvent.click(await expandRecorded());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "fixed server text",
    );
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
    expect(screen.getByText("ROW RECORDED")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Link, no money moved" }),
    ).toBeEnabled();
  });

  it("V5: while a link is pending the buttons are disabled and a second click sends nothing", async () => {
    const d = deferred<typeof ack>();
    api.linkRecorded.mockReturnValue(d.promise);
    await renderInbox();
    const btn = await expandRecorded();
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(btn).toBeDisabled();
    for (const b of screen.getAllByRole("button", { name: "Ignore" })) {
      expect(b).toBeDisabled();
    }
    d.resolve(ack);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
  });
});

describe("ignore", () => {
  const openForm = async () => {
    const onConfirmed = await renderInbox();
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    return { onConfirmed, input: screen.getByLabelText("Reason for ignoring") };
  };

  it("V3: a short reason keeps the button disabled and sends nothing", async () => {
    await openForm();
    const input = screen.getByLabelText("Reason for ignoring");
    const btn = screen.getByRole("button", { name: "Ignore transfer" });
    expect(btn).toBeDisabled();
    fireEvent.change(input, { target: { value: "  ab  " } });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("V3: with 3+ characters after trimming it is sent once, with the reason and the id", async () => {
    api.ignoreTransaction.mockResolvedValue({
      ...ack,
      reconciled_status: "ignored",
    });
    const { onConfirmed, input } = await openForm();
    fireEvent.change(input, { target: { value: "  own account move  " } });
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledTimes(1);
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 4,
      reason: "own account move",
    });
    // the reason goes to the API and nowhere else: not to the toast
    expect(JSON.stringify(toast.success.mock.calls)).not.toContain(
      "own account move",
    );
    expect(JSON.stringify(toast.error.mock.calls)).not.toContain(
      "own account move",
    );
  });

  it("V6: an ignore error shows the server's text and keeps the form", async () => {
    api.ignoreTransaction.mockRejectedValue(new Error("reason is not fit"));
    const { onConfirmed, input } = await openForm();
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "reason is not fit",
    );
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue("abc");
  });

  it("Cancel closes the form without a request", async () => {
    await openForm();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("V5: a second click on Ignore transfer while pending sends nothing", async () => {
    const d = deferred<typeof ack>();
    api.ignoreTransaction.mockReturnValue(d.promise);
    const { input } = await openForm();
    fireEvent.change(input, { target: { value: "abc" } });
    const btn = screen.getByRole("button", { name: "Ignore transfer" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(api.ignoreTransaction).toHaveBeenCalledTimes(1);
    expect(btn).toBeDisabled();
    d.resolve(ack);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });
});

describe("linked and ignored", () => {
  const linked = {
    id: 31,
    description: "ROW LINKED",
    txn_date: "2026-01-02",
    amount_idr: 100000,
    reconciled_status: "manual",
    direction: "credit",
  };
  const ignored = {
    ...linked,
    id: 32,
    description: "ROW IGNORED",
    reconciled_status: "ignored",
  };

  beforeEach(() => {
    api.getBankTransactions.mockImplementation(
      async (opts) =>
        (opts?.reconciledStatus === "manual" ? [linked] : [ignored]) as never,
    );
  });

  it("is collapsed and loads nothing until opened; then loads manual and ignored", async () => {
    await renderInbox();
    expect(api.getBankTransactions).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW LINKED");
    expect(screen.getByText("ROW IGNORED")).toBeInTheDocument();
    const statuses = api.getBankTransactions.mock.calls.map(
      (c) => c[0]?.reconciledStatus,
    );
    expect(statuses.sort()).toEqual(["ignored", "manual"]);
  });

  it("V4: reopen is sent only on click, with that row's id; lists reload, onConfirmed called", async () => {
    api.reopenTransaction.mockResolvedValue({
      ...ack,
      reconciled_status: "unmatched",
    });
    const onConfirmed = await renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW IGNORED");
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    const [, second] = screen.getAllByRole("button", { name: "Reopen" });
    const before = api.getBankTransactions.mock.calls.length;
    fireEvent.click(second);
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.reopenTransaction).toHaveBeenCalledTimes(1);
    const id = api.reopenTransaction.mock.calls[0][0];
    expect([31, 32]).toContain(id);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(api.getBankTransactions.mock.calls.length).toBe(before + 2),
    );
  });

  it("V5/V6: a pending reopen disables Reopen; an error shows the server's text", async () => {
    const d = deferred<typeof ack>();
    api.reopenTransaction.mockReturnValue(d.promise);
    const onConfirmed = await renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW LINKED");
    const [first] = screen.getAllByRole("button", { name: "Reopen" });
    fireEvent.click(first);
    fireEvent.click(first);
    expect(api.reopenTransaction).toHaveBeenCalledTimes(1);
    for (const b of screen.getAllByRole("button", { name: "Reopen" })) {
      expect(b).toBeDisabled();
    }
    d.reject(new Error("already confirmed"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already confirmed",
    );
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "Reopen" })[0]).toBeEnabled();
  });
});

describe("confirm flow", () => {
  it("V7: Match opens the existing modal and Confirm sends the same request as before", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 1,
        light: "green",
        description: "ROW GREEN",
        suggestion: candidate(),
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: [candidate()],
      recorded_candidates: [],
    });
    api.confirm.mockResolvedValue({} as never);
    const onConfirmed = vi.fn();
    render(<ReconciliationInbox onConfirmed={onConfirmed} />);
    fireEvent.click(await screen.findByText("ROW GREEN"));
    expect(await screen.findByText("Suggested")).toBeInTheDocument();
    expect(screen.getByText("Open invoices")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    expect(api.confirm).not.toHaveBeenCalled();
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm paid" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({
      bank_txn_id: 1,
      practice_id: 21,
      invoice_id: 11,
      amount_applied_idr: 500000,
      new_status: "paid",
      payment_method: "bank_transfer",
      payment_reference: "ROW GREEN",
      movement_date: "2026-01-05",
      pph_withheld_idr: 0,
    });
  });
});

describe("late answers, background reload, truncated groups", () => {
  it("V9: a late answer for row A never lands under row B", async () => {
    const a = deferred<Awaited<ReturnType<typeof api.getMatchCandidates>>>();
    const b = deferred<Awaited<ReturnType<typeof api.getMatchCandidates>>>();
    api.getMatchCandidates.mockImplementation((id: number) =>
      id === 1 ? a.promise : b.promise,
    );
    api.linkRecorded.mockResolvedValue(ack);
    await renderInbox();
    fireEvent.click(within(row(1)).getByText("ROW GREEN"));
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    b.resolve({
      txn_id: 2,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [
        candidate({ invoice_id: 52, practice_id: 62, client_name: "CONTOH B" }),
      ],
    });
    await screen.findByText("CONTOH B");
    a.resolve({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [
        candidate({ invoice_id: 51, practice_id: 61, client_name: "CONTOH A" }),
      ],
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("CONTOH A")).toBeNull();
    expect(screen.getByText("CONTOH B")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 2,
      practice_id: 62,
      invoice_id: 52,
    });
  });

  it("V9: an answer whose txn_id differs from its row is dropped", async () => {
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 99,
      transfer_amount_idr: 1,
      candidates: [],
      recorded_candidates: [candidate({ client_name: "CONTOH STRAY" })],
    });
    await renderInbox();
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    await waitFor(() => expect(api.getMatchCandidates).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("CONTOH STRAY")).toBeNull();
  });

  it("V10: after a successful ignore the row is gone at once, the rest stay, no spinner; then the server's list wins", async () => {
    api.ignoreTransaction.mockResolvedValue({
      ...ack,
      reconciled_status: "ignored",
    });
    const reload = deferred<ReturnType<typeof four>>();
    await renderInbox();
    api.getReconciliationInbox.mockReturnValue(reload.promise);
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "own account move" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));

    await waitFor(() => expect(screen.queryByText("ROW RED")).toBeNull());
    expect(screen.getByText("ROW GREEN")).toBeInTheDocument();
    expect(screen.getByText("ROW YELLOW")).toBeInTheDocument();
    expect(screen.queryByText("Loading inbox…")).toBeNull();

    reload.resolve([
      inboxItem({ txn_id: 9, light: "red", description: "ROW SERVER" }),
    ]);
    await screen.findByText("ROW SERVER");
    expect(screen.queryByText("ROW GREEN")).toBeNull();
  });

  it("V10: an older reload answering after a newer one never replaces the list", async () => {
    api.ignoreTransaction.mockResolvedValue({
      ...ack,
      reconciled_status: "ignored",
    });
    const older = deferred<ReturnType<typeof four>>();
    const newer = deferred<ReturnType<typeof four>>();
    await renderInbox();
    api.getReconciliationInbox
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const ignore = async (id: number, gone: string) => {
      fireEvent.click(within(row(id)).getByRole("button", { name: "Ignore" }));
      fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
        target: { value: "own account move" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
      await waitFor(() => expect(screen.queryByText(gone)).toBeNull());
    };
    await ignore(4, "ROW RED");
    await ignore(3, "ROW YELLOW");
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(3);

    newer.resolve([
      inboxItem({ txn_id: 9, light: "red", description: "ROW NEWER" }),
    ]);
    await screen.findByText("ROW NEWER");
    older.resolve([
      inboxItem({ txn_id: 3, light: "yellow", description: "ROW YELLOW" }),
      inboxItem({ txn_id: 8, light: "red", description: "ROW OLDER" }),
    ]);
    await older.promise;
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("ROW NEWER")).toBeInTheDocument();
    expect(screen.queryByText("ROW OLDER")).toBeNull();
    expect(screen.queryByText("ROW YELLOW")).toBeNull();
  });

  it("V10: a failed background reload keeps the current list and toasts the error", async () => {
    api.ignoreTransaction.mockResolvedValue({
      ...ack,
      reconciled_status: "ignored",
    });
    await renderInbox();
    api.getReconciliationInbox.mockRejectedValue(new Error("down"));
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Failed to load reconciliation inbox",
        "Please try again",
      ),
    );
    expect(screen.getByText("ROW GREEN")).toBeInTheDocument();
    expect(screen.getByText("ROW YELLOW")).toBeInTheDocument();
  });

  it("V11: count 9 with 5 rows shows the line with both numbers", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 2,
        light: "recorded",
        description: "ROW RECORDED",
        recorded_candidates: 9,
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 2,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [1, 2, 3, 4, 5].map((n) =>
        candidate({ invoice_id: n, practice_id: n }),
      ),
    });
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW RECORDED"));
    expect(
      await screen.findByText(
        "9 invoices have this amount; the 5 closest are shown.",
      ),
    ).toBeInTheDocument();
  });

  it("V11: count 2 with 2 rows shows no line", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 2,
        light: "recorded",
        description: "ROW RECORDED",
        recorded_candidates: 2,
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 2,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [1, 2].map((n) =>
        candidate({ invoice_id: n, practice_id: n }),
      ),
    });
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW RECORDED"));
    await screen.findByText("Already paid");
    expect(screen.queryByText(/invoices have this amount/)).toBeNull();
  });

  it("V11: zero rows but a positive count renders the heading with the line only", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 2,
        light: "yellow",
        description: "ROW YELLOW",
        open_candidates: 3,
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 2,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [],
    });
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW YELLOW"));
    expect(await screen.findByText("Open invoices")).toBeInTheDocument();
    expect(
      screen.getByText("3 invoices have this amount; the 0 closest are shown."),
    ).toBeInTheDocument();
  });
});

describe("round 3: failure state, dialog, one write at a time", () => {
  const matchResp = (id: number) => ({
    txn_id: id,
    transfer_amount_idr: 500000,
    candidates: [candidate()],
    recorded_candidates: [],
  });
  const linkedRow = {
    id: 31,
    description: "ROW LINKED",
    txn_date: "2026-01-02",
    amount_idr: 1,
    reconciled_status: "manual",
    direction: "credit",
  };

  const openDialog = async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ txn_id: 1, light: "green", description: "ROW GREEN" }),
      inboxItem({ txn_id: 4, light: "red", description: "ROW RED" }),
    ]);
    api.getMatchCandidates.mockResolvedValue(matchResp(1));
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW GREEN"));
    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    await screen.findByText("Confirm payment match");
  };

  it("C1: a failed candidates request shows the failure text, no spinner; reopening retries", async () => {
    api.getMatchCandidates.mockRejectedValueOnce(new Error("boom"));
    await renderInbox();
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    await screen.findByText(
      "Could not load the invoices for this transfer. Close the row and open it again.",
    );
    expect(screen.queryByText(/Ranking/)).toBeNull();

    api.getMatchCandidates.mockResolvedValue({
      txn_id: 2,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [candidate({ client_name: "CONTOH OK" })],
    });
    fireEvent.click(within(row(2)).getByText("ROW RECORDED")); // collapse
    fireEvent.click(within(row(2)).getByText("ROW RECORDED")); // expand again
    await screen.findByText("CONTOH OK");
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/Could not load the invoices/)).toBeNull();
  });

  it("C2: the confirm dialog survives a reload that leaves the list empty", async () => {
    // A background reload (started by an earlier link) is still pending when the
    // dialog opens; it then answers with an empty list.
    const reload = deferred<ReturnType<typeof four>>();
    api.linkRecorded.mockResolvedValue(ack);
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ txn_id: 1, light: "green", description: "ROW GREEN" }),
      inboxItem({ txn_id: 2, light: "recorded", description: "ROW RECORDED" }),
    ]);
    api.getMatchCandidates.mockImplementation(async (id: number) =>
      id === 2
        ? {
            txn_id: 2,
            transfer_amount_idr: 500000,
            candidates: [],
            recorded_candidates: [
              candidate({ invoice_id: 12, practice_id: 22 }),
            ],
          }
        : matchResp(1),
    );
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW RECORDED"));
    api.getReconciliationInbox.mockReturnValue(reload.promise);
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalled());
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    fireEvent.click(await screen.findByText("ROW GREEN"));
    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    await screen.findByText("Confirm payment match");
    reload.resolve([]);
    await screen.findByText(/No unmatched incoming transfers/);
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
  });

  it("C3a: with the dialog open Link, Ignore and Reopen are disabled and send nothing", async () => {
    api.getBankTransactions.mockResolvedValue([linkedRow] as never);
    await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = (
      await screen.findAllByRole("button", { name: "Reopen" })
    )[0];
    const ignore = within(row(4)).getByRole("button", { name: "Ignore" });
    expect(reopen).toBeDisabled();
    expect(ignore).toBeDisabled();
    expect(screen.getByRole("button", { name: "Match" })).toBeDisabled();
    fireEvent.click(reopen);
    fireEvent.click(ignore);
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();
  });

  it("C3b: while an inbox write is pending Reopen is disabled and sends nothing", async () => {
    const d = deferred<typeof ack>();
    api.linkRecorded.mockReturnValue(d.promise);
    api.getBankTransactions.mockResolvedValue([linkedRow] as never);
    await renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findAllByRole("button", { name: "Reopen" });
    fireEvent.click(await expandRecorded());
    const [reopen] = screen.getAllByRole("button", { name: "Reopen" });
    expect(reopen).toBeDisabled();
    fireEvent.click(reopen);
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    d.resolve(ack);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("C3b: while a reopen is pending Link and Ignore are disabled and send nothing", async () => {
    const d = deferred<typeof ack>();
    api.reopenTransaction.mockReturnValue(d.promise);
    api.getBankTransactions.mockResolvedValue([linkedRow] as never);
    await renderInbox();
    const link = await expandRecorded();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Reopen" }))[0],
    );
    expect(link).toBeDisabled();
    const ignore = within(row(4)).getByRole("button", { name: "Ignore" });
    expect(ignore).toBeDisabled();
    fireEvent.click(link);
    fireEvent.click(ignore);
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();
    d.resolve(ack);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("C4: a write on another row does not wipe the open ignore form", async () => {
    api.linkRecorded.mockResolvedValue(ack);
    const onConfirmed = await renderInbox();
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "typed on row four" },
    });
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue(
      "typed on row four",
    );
  });

  it("C5: the ignore form says what it does", async () => {
    await renderInbox();
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    expect(
      screen.getByText(
        "Ignoring moves no money. The transfer can be reopened under Linked and ignored.",
      ),
    ).toBeInTheDocument();
  });
});

describe("round 4: one write at a time, dialog included", () => {
  const both = {
    txn_id: 2,
    transfer_amount_idr: 500000,
    candidates: [candidate({ invoice_id: 11, practice_id: 21 })],
    recorded_candidates: [candidate({ invoice_id: 12, practice_id: 22 })],
  };
  const linkedRow = {
    id: 31,
    description: "ROW LINKED",
    txn_date: "2026-01-02",
    amount_idr: 1,
    reconciled_status: "manual",
    direction: "credit",
  };

  it("S1: with the confirm held, Close is disabled and inert, one request, no Ignore; then the dialog closes and the row is gone", async () => {
    const d = deferred<never>();
    api.confirm.mockReturnValue(d.promise);
    api.getMatchCandidates.mockResolvedValue({ ...both, txn_id: 2 });
    await renderInbox();
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    const confirmBtn = screen.getByRole("button", { name: "Confirm paid" });
    fireEvent.click(confirmBtn);
    fireEvent.click(confirmBtn);

    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toBeDisabled();
    fireEvent.click(close);
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(api.confirm).toHaveBeenCalledTimes(1);
    expect(
      within(row(2)).getByRole("button", { name: "Ignore" }),
    ).toBeDisabled();

    api.getReconciliationInbox.mockResolvedValue(
      four().filter((i) => i.txn_id !== 2),
    );
    d.resolve({} as never);
    await waitFor(() =>
      expect(screen.queryByText("Confirm payment match")).toBeNull(),
    );
    await waitFor(() => expect(screen.queryByText("ROW RECORDED")).toBeNull());
    expect(api.confirm).toHaveBeenCalledTimes(1);
  });

  it("S1b: Link then Match in the same tick: the link is sent and NO dialog opens", async () => {
    const d = deferred<typeof ack>();
    api.linkRecorded.mockReturnValue(d.promise);
    api.getMatchCandidates.mockResolvedValue({ ...both, txn_id: 2 });
    await renderInbox();
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const match = screen.getByRole("button", { name: "Match" });
    act(() => {
      link.click();
      match.click();
    });
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Confirm payment match")).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();

    api.getReconciliationInbox.mockResolvedValue(
      four().filter((i) => i.txn_id !== 2),
    );
    d.resolve(ack);
    await waitFor(() => expect(screen.queryByText("ROW RECORDED")).toBeNull());
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("B2: Match then Link in the same tick: the dialog opens and NO link is sent", async () => {
    api.getMatchCandidates.mockResolvedValue({ ...both, txn_id: 2 });
    await renderInbox();
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const match = screen.getByRole("button", { name: "Match" });
    act(() => {
      match.click();
      link.click();
    });
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(api.linkRecorded).not.toHaveBeenCalled();
  });

  it.each(["Close", "Cancel"])(
    "B1: confirm held, %s clicked in the same tick as Confirm paid: the dialog stays",
    async (name) => {
      const d = deferred<never>();
      api.confirm.mockReturnValue(d.promise);
      api.getMatchCandidates.mockResolvedValue({ ...both, txn_id: 2 });
      await renderInbox();
      fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
      fireEvent.click(await screen.findByRole("button", { name: "Match" }));
      const confirmBtn = screen.getByRole("button", { name: "Confirm paid" });
      const closer = screen.getByRole("button", { name });
      act(() => {
        confirmBtn.click();
        closer.click();
      });
      expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
      expect(api.confirm).toHaveBeenCalledTimes(1);
      act(() => {
        closer.click();
      });
      expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
      d.resolve({} as never);
      await waitFor(() =>
        expect(screen.queryByText("Confirm payment match")).toBeNull(),
      );
    },
  );

  const readyCard = async () => {
    api.previewBankStatement.mockResolvedValue(previewFixture());
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "c.csv", { type: "text/csv" })] },
    });
    return screen.findByRole("button", { name: /import 3 transactions/i });
  };

  it("S2: with the import held, Link, Ignore, Match and Reopen are disabled and send nothing; they come back after", async () => {
    const d = deferred<ReturnType<typeof importFixture>>();
    api.importBankStatement.mockReturnValue(d.promise);
    api.getMatchCandidates.mockResolvedValue({ ...both, txn_id: 2 });
    api.getBankTransactions.mockResolvedValue([linkedRow] as never);
    await renderInbox();
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = (
      await screen.findAllByRole("button", { name: "Reopen" })
    )[0];
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const match = screen.getByRole("button", { name: "Match" });
    fireEvent.click(await readyCard());

    const ignore = within(row(4)).getByRole("button", { name: "Ignore" });
    for (const b of [link, match, ignore, reopen]) expect(b).toBeDisabled();
    for (const b of [link, match, ignore, reopen]) fireEvent.click(b);
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    expect(screen.queryByText("Confirm payment match")).toBeNull();
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();

    d.resolve(importFixture());
    await screen.findByText(/new transactions stored/);
    await waitFor(() => expect(ignore).toBeEnabled());
  });

  it("S2: with a link held, the Import button is disabled and sends nothing; it comes back after", async () => {
    const d = deferred<typeof ack>();
    api.linkRecorded.mockReturnValue(d.promise);
    api.importBankStatement.mockResolvedValue(importFixture());
    await renderInbox();
    const importBtn = await readyCard();
    fireEvent.click(await expandRecorded());
    expect(importBtn).toBeDisabled();
    fireEvent.click(importBtn);
    expect(api.importBankStatement).not.toHaveBeenCalled();
    d.resolve(ack);
    await waitFor(() => expect(importBtn).toBeEnabled());
    fireEvent.click(importBtn);
    await waitFor(() =>
      expect(api.importBankStatement).toHaveBeenCalledTimes(1),
    );
  });
});

describe("round 6", () => {
  const openForm = (id: number, text?: string) => {
    fireEvent.click(within(row(id)).getByRole("button", { name: "Ignore" }));
    if (text !== undefined)
      fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
        target: { value: text },
      });
  };
  const sendIgnore = () =>
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
  const ignoreCalls = () => api.ignoreTransaction.mock.calls.map((c) => c[0]);

  it("F1: Link on row 1 then Ignore on row 2 in one tick: row 2's form is never half-opened or wiped", async () => {
    api.linkRecorded.mockResolvedValue(ack);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [candidate({ invoice_id: 12, practice_id: 22 })],
    });
    await renderInbox();
    fireEvent.click(within(row(1)).getByText("ROW GREEN"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const ignore2 = within(row(3)).getByRole("button", { name: "Ignore" });
    act(() => {
      link.click();
      ignore2.click();
    });
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("F2: Ignore transfer then the form's Cancel in one tick, server refuses: error shown, form and reason still there", async () => {
    const d = deferred<typeof ack>();
    api.ignoreTransaction.mockReturnValue(d.promise);
    await renderInbox();
    openForm(4, "typed reason");
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      send.click();
      cancel.click();
    });
    d.reject(new Error("fixed refusal"));
    expect(await screen.findByRole("alert")).toHaveTextContent("fixed refusal");
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue(
      "typed reason",
    );
  });

  it("F2: Match then Ignore in one tick: the dialog opens and no form opens", async () => {
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: [candidate()],
      recorded_candidates: [],
    });
    await renderInbox();
    fireEvent.click(within(row(1)).getByText("ROW GREEN"));
    const match = await screen.findByRole("button", { name: "Match" });
    const ignore = within(row(4)).getByRole("button", { name: "Ignore" });
    act(() => {
      match.click();
      ignore.click();
    });
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(screen.queryByLabelText("Reason for ignoring")).toBeNull();
  });

  it('F5: an empty memo sends payment_reference ""; a memo with text sends it', async () => {
    api.confirm.mockResolvedValue({} as never);
    for (const [memo, expected] of [
      ["", ""],
      ["MEMO CONTOH", "MEMO CONTOH"],
    ] as const) {
      api.confirm.mockClear();
      api.getReconciliationInbox.mockResolvedValue([
        inboxItem({ txn_id: 1, light: "green", description: memo }),
      ]);
      api.getMatchCandidates.mockResolvedValue({
        txn_id: 1,
        transfer_amount_idr: 500000,
        candidates: [candidate()],
        recorded_candidates: [],
      });
      const { unmount } = render(<ReconciliationInbox onConfirmed={vi.fn()} />);
      fireEvent.click(await screen.findByText(memo || "(no memo)"));
      fireEvent.click(await screen.findByRole("button", { name: "Match" }));
      fireEvent.click(screen.getByRole("button", { name: "Confirm paid" }));
      await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
      expect(api.confirm.mock.calls[0][0].payment_reference).toBe(expected);
      unmount();
    }
  });

  it("F6: the reason is counted in code points after collapsing whitespace, and sent collapsed", async () => {
    api.ignoreTransaction.mockResolvedValue(ack);
    await renderInbox();
    openForm(4, "\u{1F600}\u{1F600}");
    const btn = screen.getByRole("button", { name: "Ignore transfer" });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "\u{1F600}\u{1F600}\u{1F600}" },
    });
    expect(btn).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "  a  \t b " },
    });
    expect(btn).toBeEnabled();
    sendIgnore();
    await waitFor(() => expect(ignoreCalls()).toHaveLength(1));
    expect(ignoreCalls()[0]).toEqual({ bank_txn_id: 4, reason: "a b" });
  });

  it("F7: a failed first load is not an empty inbox; Try again loads; a successful empty load says Inbox zero", async () => {
    api.getReconciliationInbox.mockRejectedValueOnce(new Error("down"));
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    expect(
      await screen.findByText("Could not load the inbox."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Inbox zero/)).toBeNull();
    api.getReconciliationInbox.mockResolvedValue([]);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText(/Inbox zero/);
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
  });

  it("F7: first load held, a second load fails, the first is dropped: failure text, not Inbox zero", async () => {
    const first = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox.mockReturnValueOnce(first.promise);
    api.getReconciliationInbox.mockRejectedValueOnce(new Error("down"));
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockResolvedValue(importFixture());
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "c.csv", { type: "text/csv" })] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    expect(
      await screen.findByText("Could not load the inbox."),
    ).toBeInTheDocument();
    first.resolve([]);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("Could not load the inbox.")).toBeInTheDocument();
    expect(screen.queryByText(/Inbox zero/)).toBeNull();
  });

  it("F7: a failed reload with rows on screen keeps the rows", async () => {
    api.ignoreTransaction.mockResolvedValue(ack);
    await renderInbox();
    api.getReconciliationInbox.mockRejectedValue(new Error("down"));
    openForm(4, "abc");
    sendIgnore();
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByText("ROW GREEN")).toBeInTheDocument();
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
  });
});

describe("round 7", () => {
  const oneRow = () => [
    inboxItem({ txn_id: 4, light: "red", description: "ROW RED" }),
  ];
  const ignoreLast = async () => {
    api.ignoreTransaction.mockResolvedValue(ack);
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
  };
  const heldReload = () => {
    const d = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox.mockReturnValue(d.promise);
    return d;
  };

  it("N1: last row leaves with the reload held: loading line, then the load result decides", async () => {
    api.getReconciliationInbox.mockResolvedValue(oneRow());
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    await screen.findByText("ROW RED");
    const d = heldReload();
    await ignoreLast();
    await screen.findByText("Loading inbox…");
    expect(screen.queryByText(/Inbox zero/)).toBeNull();
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
    d.resolve([]);
    await screen.findByText(/Inbox zero/);
  });

  it("N1: the held reload then fails: failure text", async () => {
    api.getReconciliationInbox.mockResolvedValue(oneRow());
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    await screen.findByText("ROW RED");
    let rej!: (e: Error) => void;
    api.getReconciliationInbox.mockReturnValue(
      new Promise((_, r) => {
        rej = r;
      }) as never,
    );
    await ignoreLast();
    await screen.findByText("Loading inbox…");
    rej(new Error("down"));
    await screen.findByText("Could not load the inbox.");
  });

  it("N1: after a failed reload, the last row leaves and a new reload is in flight: loading line, not the failure", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ txn_id: 4, light: "red", description: "ROW RED" }),
      inboxItem({ txn_id: 3, light: "yellow", description: "ROW YELLOW" }),
    ]);
    api.linkRecorded.mockResolvedValue(ack);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 3,
      transfer_amount_idr: 1,
      candidates: [],
      recorded_candidates: [candidate({ invoice_id: 12, practice_id: 22 })],
    });
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    await screen.findByText("ROW RED");
    api.getReconciliationInbox.mockRejectedValueOnce(new Error("down"));
    fireEvent.click(within(row(3)).getByText("ROW YELLOW"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByText("ROW RED")).toBeInTheDocument();
    heldReload();
    await ignoreLast();
    await screen.findByText("Loading inbox…");
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
  });

  const formState = () => screen.queryByLabelText("Reason for ignoring");

  it("N2: the form's Cancel then Ignore transfer in one tick sends nothing", async () => {
    await renderInbox();
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "abcdef" },
    });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    act(() => {
      cancel.click();
      send.click();
    });
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    expect(formState()).toBeNull();
  });

  it("N2/N4: Ignore on row 2 then Cancel on row 3 (its form was open): the form ends on row 2, none opens on row 3", async () => {
    await renderInbox();
    fireEvent.click(within(row(3)).getByRole("button", { name: "Ignore" }));
    const cancel3 = screen.getByRole("button", { name: "Cancel" });
    const ignore2 = within(row(2)).getByRole("button", { name: "Ignore" });
    act(() => {
      ignore2.click();
      cancel3.click();
    });
    expect(
      within(row(2)).getByLabelText("Reason for ignoring"),
    ).toBeInTheDocument();
    expect(within(row(3)).queryByLabelText("Reason for ignoring")).toBeNull();
  });

  it("N2: typing then Ignore transfer in one tick sends the latest text", async () => {
    api.ignoreTransaction.mockResolvedValue(ack);
    await renderInbox();
    fireEvent.click(within(row(4)).getByRole("button", { name: "Ignore" }));
    const input = screen.getByLabelText("Reason for ignoring");
    fireEvent.change(input, { target: { value: "first text" } });
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    act(() => {
      fireEvent.change(input, { target: { value: "latest text" } });
      send.click();
    });
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 4,
      reason: "latest text",
    });
  });

  it("N3b: Link, Import (refused) and a file choice in one tick: the preview is sent", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValue(held.promise);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 2,
      transfer_amount_idr: 1,
      candidates: [],
      recorded_candidates: [candidate({ invoice_id: 12, practice_id: 22 })],
    });
    api.previewBankStatement.mockResolvedValue(previewFixture());
    await renderInbox();
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "a.csv", { type: "text/csv" })] },
    });
    const importBtn = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    fireEvent.click(within(row(2)).getByText("ROW RECORDED"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    api.previewBankStatement.mockClear();
    act(() => {
      link.click();
      importBtn.click();
      fireEvent.change(screen.getByTestId("bank-statement-input"), {
        target: { files: [new File(["y"], "b.csv", { type: "text/csv" })] },
      });
    });
    expect(api.importBankStatement).not.toHaveBeenCalled();
    expect(api.previewBankStatement).toHaveBeenCalledTimes(1);
    held.resolve(ack);
  });
});

describe("candidate identity on the row", () => {
  const open = async (cands: ReturnType<typeof candidate>[], recorded = []) => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 1,
        light: "green",
        description: "ROW GREEN",
        open_candidates: cands.length,
        recorded_candidates: recorded.length,
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: cands,
      recorded_candidates: recorded,
    });
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    fireEvent.click(await screen.findByText("ROW GREEN"));
    await screen.findAllByTestId("candidate-identity");
  };

  it("shows number, date and service on open and on recorded candidates", async () => {
    const id = {
      invoice_number: "INV-000000-00001",
      invoice_issued_at: "2026-01-10T00:00:00+00:00",
      service_name: "Example service",
    };
    await open([candidate({ invoice_id: 11, ...id })], [
      candidate({
        invoice_id: 12,
        practice_id: 22,
        ...id,
        invoice_number: "INV-000000-00002",
      }),
    ] as never);
    const texts = screen
      .getAllByTestId("candidate-identity")
      .map((e) => e.textContent);
    expect(texts).toEqual([
      "INV-000000-00001 · 2026-01-10 · Example service",
      "INV-000000-00002 · 2026-01-10 · Example service",
    ]);
  });

  it("shows No invoice number for null and absent, without date or service", async () => {
    await open([
      candidate({
        invoice_id: 11,
        invoice_number: null,
        invoice_issued_at: null,
        service_name: null,
      }),
      candidate({ invoice_id: 13, practice_id: 23 }),
    ]);
    const texts = screen
      .getAllByTestId("candidate-identity")
      .map((e) => e.textContent);
    expect(texts).toEqual(["No invoice number", "No invoice number"]);
  });
});

describe("historical mode: recorded without emails", () => {
  const ORDINARY = {
    bank_txn_id: 1,
    practice_id: 21,
    invoice_id: 11,
    amount_applied_idr: 500000,
    new_status: "paid",
    payment_method: "bank_transfer",
    payment_reference: "ROW GREEN",
    movement_date: "2026-01-05",
    pph_withheld_idr: 0,
  };
  beforeEach(() => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 1,
        light: "green",
        description: "ROW GREEN",
        suggestion: candidate(),
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      txn_id: 1,
      transfer_amount_idr: 500000,
      candidates: [candidate()],
      recorded_candidates: [],
    });
    api.confirm.mockResolvedValue({} as never);
  });
  const toggle = () =>
    screen.getByRole("checkbox", { name: /Historical mode: no emails/ });
  const openDialog = async () => {
    fireEvent.click(await screen.findByText("ROW GREEN"));
    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
  };

  it("is off by default, and the dialog says nothing about emails (innocence)", async () => {
    render(<ReconciliationInbox onConfirmed={vi.fn()} />);
    expect(await screen.findByText("ROW GREEN")).toBeInTheDocument();
    expect(toggle()).not.toBeChecked();
    await openDialog();
    expect(screen.queryByText("Emails", { selector: "dt" })).toBeNull();
    expect(screen.queryByText(/Historical mode: no email goes/)).toBeNull();
  });

  it("on: the dialog says no emails and Confirm sends historical: true with the same request", async () => {
    const onConfirmed = vi.fn();
    render(<ReconciliationInbox onConfirmed={onConfirmed} />);
    expect(await screen.findByText("ROW GREEN")).toBeInTheDocument();
    fireEvent.click(toggle());
    expect(toggle()).toBeChecked();
    await openDialog();
    expect(
      screen.getByText("Emails", { selector: "dt" }).parentElement,
    ).toHaveTextContent("None: historical mode");
    expect(
      screen.getByText(
        /Historical mode: no email goes to the client or the team\./,
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm paid" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({ ...ORDINARY, historical: true });
  });

  it("the dialog keeps the mode it opened with: ticking the box under it changes nothing", async () => {
    const onConfirmed = vi.fn();
    render(<ReconciliationInbox onConfirmed={onConfirmed} />);
    expect(await screen.findByText("ROW GREEN")).toBeInTheDocument();
    await openDialog();
    fireEvent.click(toggle());
    expect(screen.queryByText("Emails", { selector: "dt" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm paid" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith(ORDINARY);
  });
});
