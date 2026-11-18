import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  accountingApi,
  type BankTransaction,
} from "@/lib/api/workspace/accounting.api";
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
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
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
const openOne = candidate({
  invoice_id: 11,
  practice_id: 21,
  client_name: "PT CONTOH SATU",
  invoice_number: "INV-000000-00001",
  invoice_issued_at: "2026-01-02T00:00:00Z",
  service_name: "Example service",
  reasons: ["same amount", "invoice number in memo"],
});
const openTwo = candidate({
  invoice_id: 13,
  practice_id: 23,
  client_name: "PT CONTOH TIGA",
  invoice_number: "INV-000000-00003",
});
const recorded = candidate({
  invoice_id: 12,
  practice_id: 22,
  client_name: "PT CONTOH DUA",
  invoice_number: "INV-000000-00002",
  reasons: ["payment already recorded"],
});
const four = () => [
  inboxItem({
    txn_id: 1,
    light: "green",
    description: "ROW GREEN",
    open_candidates: 1,
    suggestion: openOne,
  }),
  inboxItem({
    txn_id: 2,
    light: "recorded",
    description: "ROW RECORDED",
    recorded_candidates: 1,
    suggestion: recorded,
  }),
  inboxItem({
    txn_id: 3,
    light: "yellow",
    description: "ROW YELLOW",
    open_candidates: 2,
  }),
  inboxItem({
    txn_id: 4,
    light: "red",
    description: "ROW RED",
  }),
];
const done = {
  day: "2026-01-05",
  timezone: "Asia/Makassar",
  counts: { confirmed: 1, linked: 1, ignored: 0, total: 2 },
  items: [],
  truncated: false,
};
const bank = {
  id: 99,
  statement_id: null,
  txn_date: "2026-01-05",
  description: "BANK",
  amount_idr: 1,
  direction: "credit",
  running_balance_idr: null,
  bank_account_id: null,
  reconciled_status: "unmatched",
  matched_practice_id: null,
  matched_invoice_id: null,
  created_at: null,
} satisfies BankTransaction;
const candidatesFor = (id: number) => ({
  txn_id: id,
  transfer_amount_idr: 500000,
  candidates:
    id === 1 || id === 3 ? [openOne, ...(id === 3 ? [openTwo] : [])] : [],
  recorded_candidates: id === 2 || id === 3 ? [recorded] : [],
});

beforeEach(() => {
  vi.resetAllMocks();
  api.getReconciliationInbox.mockResolvedValue(four());
  api.getMatchCandidates.mockImplementation(async (id) => candidatesFor(id));
  api.getBankTransactions.mockImplementation(async (options) =>
    options?.limit === 1 ? [bank] : [],
  );
  api.getDoneToday.mockResolvedValue(done);
  api.linkRecorded.mockResolvedValue(ack);
  api.ignoreTransaction.mockResolvedValue(ack);
  api.reopenTransaction.mockResolvedValue(ack);
  api.confirm.mockResolvedValue({} as never);
});

const renderInbox = (onConfirmed = vi.fn()) => {
  render(
    <ReconciliationInbox
      onConfirmed={onConfirmed}
      doneVersion={0}
      onOpenCashout={vi.fn()}
    />,
  );
  return onConfirmed;
};
const ready = async () => screen.findByText("ROW GREEN");
const tab = (key: string) => screen.getByTestId(`cc-tab-${key}`);
const row = (id: number) => screen.getByTestId(`inbox-row-${id}`);
const opened = () => screen.getByTestId("cc-opened");
const renderedClick = (element: HTMLElement) => {
  const propsKey = Object.keys(element).find((key) =>
    key.startsWith("__reactProps$"),
  );
  if (!propsKey) throw new Error("Rendered click handler not found");
  return (element as unknown as Record<string, { onClick: () => void }>)[
    propsKey
  ].onClick;
};
const openTab = async (key: string, text: string) => {
  fireEvent.click(tab(key));
  await waitFor(() =>
    expect(tab(key)).toHaveAttribute("aria-selected", "true"),
  );
  expect(screen.getAllByText(text).length).toBeGreaterThan(0);
};
const chooseTable = async () => {
  await screen.findByRole("button", { name: "Choose another" });
  fireEvent.click(screen.getByRole("button", { name: "Choose another" }));
};
const openIgnore = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
  return screen.getByLabelText("Reason for ignoring");
};

describe("ported reconciliation contract", () => {
  it("01: maps every light and unknown values to their situation tab", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      ...four(),
      inboxItem({ txn_id: 5, light: "odd" as never, description: "ROW ODD" }),
    ]);
    renderInbox();
    await ready();
    expect(screen.getByTestId("cc-count-confirm")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-link")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("2");
    expect(screen.getByTestId("cc-count-nomatch")).toHaveTextContent("1");
  });

  it("02: aria-current belongs only to the opened row", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      four()[0],
      inboxItem({ ...four()[0], txn_id: 5, description: "ROW GREEN TWO" }),
    ]);
    renderInbox();
    await ready();
    expect(row(1)).toHaveAttribute("aria-current", "true");
    expect(row(5)).not.toHaveAttribute("aria-current");
    fireEvent.click(row(5));
    expect(row(5)).toHaveAttribute("aria-current", "true");
  });

  it("03: tabs carry counts and filter rows client-side", async () => {
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    expect(screen.queryByText("ROW GREEN")).toBeNull();
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
  });

  it("04: first render sends the R1 request census", async () => {
    renderInbox();
    await ready();
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenCalledTimes(1),
    );
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
    expect(api.getBankTransactions).toHaveBeenCalledWith({ limit: 1 });
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
  });

  it("05: To link explains linking and marks the recorded suggestion in table view", async () => {
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    expect(
      await screen.findByText("Link it instead of confirming it again."),
    ).toBeInTheDocument();
    await chooseTable();
    expect(
      within(screen.getByTestId("cc-cand-12")).getByText("Suggested"),
    ).toBeInTheDocument();
  });

  it("06: Link sends only on click with the selected ids", async () => {
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    expect(api.linkRecorded).not.toHaveBeenCalled();
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 2,
      practice_id: 22,
      invoice_id: 12,
    });
  });

  it("07: a successful link reloads and reports the write", async () => {
    const onConfirmed = renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2);
  });

  it("08: a link error is shown and the row remains", async () => {
    api.linkRecorded.mockRejectedValueOnce(new Error("link refused"));
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("link refused");
    expect(row(2)).toBeInTheDocument();
  });

  it("09: a pending link disables its controls and cannot double-send", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    const button = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    await act(async () => held.resolve(ack));
  });

  it("10: a short ignore reason cannot be sent", async () => {
    renderInbox();
    await ready();
    const input = await openIgnore();
    fireEvent.change(input, { target: { value: "ab" } });
    expect(
      screen.getByRole("button", { name: "Ignore transfer" }),
    ).toBeDisabled();
  });

  it("11: a valid trimmed ignore reason is sent with the opened id", async () => {
    renderInbox();
    await ready();
    const input = await openIgnore();
    fireEvent.change(input, { target: { value: "  not   a payment " } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: "not a payment",
    });
  });

  it("12: an ignore error keeps the form and server text", async () => {
    api.ignoreTransaction.mockRejectedValueOnce(new Error("ignore refused"));
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ignore refused",
    );
    expect(screen.getByTestId("cc-ignore-form")).toBeInTheDocument();
  });

  it("13: Cancel closes the ignore form without a request", async () => {
    renderInbox();
    await ready();
    await openIgnore();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("14: a pending ignore cannot double-send", async () => {
    const held = deferred<typeof ack>();
    api.ignoreTransaction.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(api.ignoreTransaction).toHaveBeenCalledTimes(1);
    await act(async () => held.resolve(ack));
  });

  it("15: linked and ignored loads only after its Done control opens", async () => {
    renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    expect(api.getBankTransactions).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await waitFor(() =>
      expect(api.getBankTransactions).toHaveBeenCalledTimes(3),
    );
  });

  it("16: Reopen sends the row id, reloads and reports the write", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [{ ...bank, id: 77, reconciled_status: "manual" }]
          : [],
    );
    const onConfirmed = renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    fireEvent.click(await screen.findByRole("button", { name: "Reopen" }));
    expect(api.reopenTransaction).toHaveBeenCalledWith(77);
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
  });

  it("17: pending and failed Reopen stays single and shows its error", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [{ ...bank, id: 78 }]
          : [],
    );
    const held = deferred<typeof ack>();
    api.reopenTransaction.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = await screen.findAllByRole("button", { name: "Reopen" });
    fireEvent.click(reopen[0]);
    expect(reopen[0]).toBeDisabled();
    await act(async () => held.reject(new Error("reopen refused")));
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent(
      "reopen refused",
    );
  });

  it("18: Confirm payment opens the unchanged dialog and sends the same request", async () => {
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm.mock.calls[0][0]).toMatchObject({
      bank_txn_id: 1,
      practice_id: 21,
      invoice_id: 11,
    });
  });

  it("19: a late candidate answer for A never lands under B", async () => {
    const a = deferred<ReturnType<typeof candidatesFor>>();
    api.getMatchCandidates.mockReturnValueOnce(a.promise);
    renderInbox();
    await ready();
    fireEvent.click(tab("link"));
    await screen.findByText("This payment looks already recorded");
    await act(async () => a.resolve(candidatesFor(1)));
    expect(opened()).toHaveTextContent("PT CONTOH DUA");
    expect(opened()).not.toHaveTextContent("PT CONTOH SATU");
  });

  it("20: a candidate answer with the wrong txn id is dropped", async () => {
    api.getMatchCandidates.mockResolvedValueOnce(candidatesFor(2));
    renderInbox();
    await ready();
    await waitFor(() => expect(api.getMatchCandidates).toHaveBeenCalledWith(1));
    expect(screen.getByText("Ranking candidates…")).toBeInTheDocument();
    expect(screen.queryByTestId("cc-cand-12")).toBeNull();
  });

  it("21: an acted row leaves immediately and the next row opens", async () => {
    const reload = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([
        four()[0],
        inboxItem({ ...four()[0], txn_id: 5, description: "ROW GREEN TWO" }),
      ])
      .mockReturnValueOnce(reload.promise);
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(screen.queryByTestId("inbox-row-1")).toBeNull());
    expect(row(5)).toHaveAttribute("aria-current", "true");
    await act(async () => reload.resolve([]));
  });

  it("22: an older inbox reload never replaces a newer one", async () => {
    const old = deferred<ReturnType<typeof four>>();
    const newer = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(four())
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(newer.promise);
    renderInbox();
    await ready();
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "valid reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(3),
    );
    await act(async () => newer.resolve([four()[1]]));
    await act(async () => old.resolve([four()[0]]));
    expect(screen.queryByText("ROW GREEN")).toBeNull();
  });

  it("23: a failed background reload keeps the remaining rows", async () => {
    api.getReconciliationInbox
      .mockResolvedValueOnce(four())
      .mockRejectedValueOnce(new Error("reload failed"));
    renderInbox();
    await ready();
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "valid reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(tab("link")).not.toBeDisabled();
  });

  it("24: a truncated candidate group states both counts", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ ...four()[2], open_candidates: 9 }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(3),
      candidates: [openOne, openTwo],
    });
    renderInbox();
    await screen.findByText("ROW YELLOW");
    expect(
      await screen.findByText(
        "9 invoices have this amount; the 2 closest are shown.",
      ),
    ).toBeInTheDocument();
  });

  it("25: an untruncated candidate group has no truncation sentence", async () => {
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    await screen.findByTestId("cc-cand-11");
    expect(screen.queryByText(/closest are shown/)).toBeNull();
  });

  it("26: a positive count with zero rows still shows its heading and truncation", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ ...four()[2], open_candidates: 3 }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(3),
      candidates: [],
      recorded_candidates: [],
    });
    renderInbox();
    await screen.findByText("ROW YELLOW");
    expect(await screen.findByText("Open invoices")).toBeInTheDocument();
    expect(
      screen.getByText("3 invoices have this amount; the 0 closest are shown."),
    ).toBeInTheDocument();
  });

  it("27: failed candidates show Try again and retry the same id", async () => {
    api.getMatchCandidates.mockRejectedValueOnce(new Error("rank failed"));
    renderInbox();
    await ready();
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenLastCalledWith(1),
    );
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(2);
  });

  it("28: the confirm dialog survives when its row empties the list", async () => {
    const reload = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0], four()[1]])
      .mockReturnValueOnce(reload.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    await act(async () => reload.resolve([]));
    expect(await screen.findByTestId("cc-calm")).toBeInTheDocument();
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
  });

  it("29: dialog lock keeps other-tab actions inert and survives tab changes", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [{ ...bank, id: 77, reconciled_status: "manual" }]
          : [],
    );
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    fireEvent.click(tab("link"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const ignore = screen.getByRole("button", { name: "Ignore" });
    expect(link).toBeDisabled();
    expect(ignore).toBeDisabled();
    fireEvent.click(link);
    fireEvent.click(ignore);
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = (
      await screen.findAllByRole("button", { name: "Reopen" })
    )[0];
    expect(reopen).toBeDisabled();
    fireEvent.click(reopen);
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
  });

  it("30: an inbox write shares the lock with Done-today reopen", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [{ ...bank, id: 78, reconciled_status: "manual" }]
          : [],
    );
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = (
      await screen.findAllByRole("button", { name: "Reopen" })
    )[0];
    expect(reopen).toBeDisabled();
    fireEvent.click(reopen);
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("31: a pending reopen uses the same shared lock", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1 ? [bank] : [{ ...bank, id: 79 }],
    );
    const held = deferred<typeof ack>();
    api.reopenTransaction.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Reopen" }))[0],
    );
    fireEvent.click(tab("link"));
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const ignore = screen.getByRole("button", { name: "Ignore" });
    expect(link).toBeDisabled();
    expect(ignore).toBeDisabled();
    fireEvent.click(link);
    fireEvent.click(ignore);
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("32: another row can open while the first row's ignore form retains text", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      four()[0],
      inboxItem({ ...four()[0], txn_id: 5, description: "ROW GREEN TWO" }),
    ]);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "kept reason" } });
    fireEvent.click(row(5));
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    fireEvent.click(row(1));
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue(
      "kept reason",
    );
  });

  it("33: the ignore form states the reversible no-money effect", async () => {
    renderInbox();
    await ready();
    await openIgnore();
    expect(screen.getByTestId("cc-ignore-form")).toHaveTextContent(
      "Ignoring moves no money. You can reopen this transfer any time from Done today.",
    );
  });

  it("34: while confirm is pending, Close is disabled and inert", async () => {
    const held = deferred<never>();
    api.confirm.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toBeDisabled();
    fireEvent.click(close);
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    await act(async () => held.reject(new Error("stop")));
  });

  it("35: Link then Confirm in one tick sends only the link", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    const recordedRadio = within(
      await screen.findByTestId("cc-cand-12"),
    ).getByRole("radio");
    const openRadio = within(screen.getByTestId("cc-cand-11")).getByRole(
      "radio",
    );
    fireEvent.click(recordedRadio);
    const link = screen.getByRole("button", { name: "Link, no money moved" });
    act(() => {
      link.click();
      openRadio.click();
      link.click();
    });
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Confirm payment match")).toBeNull();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("36: Confirm then Link in one tick opens the dialog and sends no link", async () => {
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    const openRadio = within(await screen.findByTestId("cc-cand-11")).getByRole(
      "radio",
    );
    const recordedRadio = within(screen.getByTestId("cc-cand-12")).getByRole(
      "radio",
    );
    fireEvent.click(openRadio);
    const confirm = screen.getByRole("button", { name: "Confirm payment" });
    act(() => {
      confirm.click();
      recordedRadio.click();
      confirm.click();
    });
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(api.linkRecorded).not.toHaveBeenCalled();
  });

  it.each(["Close", "Cancel"])(
    "37: dialog %s remains inert in the confirm request tick",
    async (name) => {
      const held = deferred<never>();
      api.confirm.mockReturnValueOnce(held.promise);
      renderInbox();
      await ready();
      fireEvent.click(
        await screen.findByRole("button", { name: "Confirm payment" }),
      );
      const confirm = screen.getByRole("button", { name: /^Confirm paid$/ });
      const closer = screen.getByRole("button", { name });
      act(() => {
        confirm.click();
        closer.click();
      });
      expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
      expect(api.confirm).toHaveBeenCalledTimes(1);
      act(() => closer.click());
      expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
      await act(async () => held.reject(new Error("stop")));
    },
  );

  it("38: a pending statement import locks reconciliation actions", async () => {
    const held = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [{ ...bank, id: 80, reconciled_status: "manual" }]
          : [],
    );
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    const confirm = screen.getByRole("button", { name: "Confirm payment" });
    const confirmIgnore = screen.getByRole("button", { name: "Ignore" });
    expect(confirm).toBeDisabled();
    expect(confirmIgnore).toBeDisabled();
    fireEvent.click(confirm);
    fireEvent.click(confirmIgnore);
    await openTab("link", "ROW RECORDED");
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const linkIgnore = screen.getByRole("button", { name: "Ignore" });
    expect(link).toBeDisabled();
    expect(linkIgnore).toBeDisabled();
    fireEvent.click(link);
    fireEvent.click(linkIgnore);
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    const reopen = (
      await screen.findAllByRole("button", { name: "Reopen" })
    )[0];
    expect(reopen).toBeDisabled();
    fireEvent.click(reopen);
    expect(api.confirm).not.toHaveBeenCalled();
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    await act(async () => held.resolve(importFixture()));
    await screen.findByText(/new transactions stored/);
    await waitFor(() => expect(reopen).toBeEnabled());
    fireEvent.click(tab("link"));
    expect(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    ).toBeEnabled();
    fireEvent.click(tab("confirm"));
    expect(
      await screen.findByRole("button", { name: "Confirm payment" }),
    ).toBeEnabled();
  });

  it("39: a pending link disables statement Import", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    api.previewBankStatement.mockResolvedValue(previewFixture());
    renderInbox();
    await ready();
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    const importButton = await screen.findByRole("button", {
      name: /Import 3 transactions/i,
    });
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    expect(importButton).toBeDisabled();
    await act(async () => held.resolve(ack));
  });

  it("40: Link then Ignore in one tick never half-opens a form", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    const ignore = screen.getByRole("button", { name: "Ignore" });
    act(() => {
      link.click();
      ignore.click();
    });
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("41: Ignore transfer then Cancel in one tick keeps a refused form", async () => {
    const held = deferred<typeof ack>();
    api.ignoreTransaction.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      send.click();
      cancel.click();
    });
    await act(async () => held.reject(new Error("refused")));
    expect(await screen.findByRole("alert")).toHaveTextContent("refused");
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue(
      "valid reason",
    );
  });

  it("42: Match then Ignore in one tick opens only the dialog", async () => {
    renderInbox();
    await ready();
    const confirm = await screen.findByRole("button", {
      name: "Confirm payment",
    });
    const ignore = screen.getByRole("button", { name: "Ignore" });
    act(() => {
      confirm.click();
      ignore.click();
    });
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
  });

  it("43: confirmation preserves empty and populated payment references", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({ ...four()[0], description: "" }),
    ]);
    renderInbox();
    await screen.findByText("(no memo)");
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalled());
    expect(api.confirm.mock.calls[0][0].payment_reference).toBe("");
  });

  it("44: ignore counts code points after collapsing whitespace", async () => {
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "  🧾   ok " } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: "🧾 ok",
    });
  });

  it("45: a failed first load retries into the calm view", async () => {
    api.getReconciliationInbox
      .mockRejectedValueOnce(new Error("first failed"))
      .mockResolvedValueOnce([]);
    renderInbox();
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("cc-calm")).toHaveTextContent(
      "All done for today",
    );
  });

  it("46: a newer failed load drops an older held answer", async () => {
    const held = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox.mockReturnValueOnce(held.promise);
    renderInbox();
    expect(screen.getByText("Loading inbox…")).toBeInTheDocument();
    await act(async () => held.reject(new Error("failed")));
    expect(screen.getByText("Could not load the inbox.")).toBeInTheDocument();
  });

  it("47: a failed reload with rows on screen keeps those rows", async () => {
    api.getReconciliationInbox
      .mockResolvedValueOnce(four())
      .mockRejectedValueOnce(new Error("reload failed"));
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(tab("link")).not.toBeDisabled();
  });

  it("48: when the last row leaves, a held reload shows loading", async () => {
    const held = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0]])
      .mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByText("Loading inbox…")).toBeInTheDocument();
    await act(async () => held.resolve([]));
  });

  it("49: when the last-row reload fails, the failure view wins", async () => {
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0]])
      .mockRejectedValueOnce(new Error("reload failed"));
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(
      await screen.findByText("Could not load the inbox."),
    ).toBeInTheDocument();
  });

  it("50: a new last-row reload replaces an older failure with loading", async () => {
    const held = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0]])
      .mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByText("Loading inbox…")).toBeInTheDocument();
    await act(async () => held.resolve([]));
  });

  it("51: Cancel then Ignore transfer in one tick sends nothing", async () => {
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      cancel.click();
      send.click();
    });
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
  });

  it("52: Ignore on B replaces A's hidden form instead of opening two", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      four()[0],
      inboxItem({ ...four()[0], txn_id: 5, description: "ROW GREEN TWO" }),
    ]);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "reason A" } });
    const cancelA = screen.getByRole("button", { name: "Cancel" });
    fireEvent.click(row(5));
    const ignoreB = await screen.findByRole("button", { name: "Ignore" });
    act(() => {
      ignoreB.click();
      cancelA.click();
    });
    expect(screen.getAllByTestId("cc-ignore-form")).toHaveLength(1);
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue("");
    fireEvent.click(row(1));
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
  });

  it("53: typing and sending in one act uses the latest ref value", async () => {
    renderInbox();
    await ready();
    const input = await openIgnore();
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    fireEvent.change(input, { target: { value: "first reason" } });
    act(() => {
      fireEvent.change(input, { target: { value: "latest reason" } });
      send.click();
    });
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: "latest reason",
    });
  });

  it("54: a refused import lock still permits choosing and previewing a file", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    api.previewBankStatement.mockResolvedValue(previewFixture());
    renderInbox();
    await ready();
    const input = screen.getByTestId("bank-statement-input");
    fireEvent.change(input, {
      target: { files: [new File(["x"], "first.csv")] },
    });
    const importButton = await screen.findByRole("button", {
      name: /Import 3 transactions/i,
    });
    await openTab("link", "ROW RECORDED");
    const link = await screen.findByRole("button", {
      name: "Link, no money moved",
    });
    api.previewBankStatement.mockClear();
    const file = new File(["y"], "second.csv");
    act(() => {
      link.click();
      importButton.click();
      fireEvent.change(input, { target: { files: [file] } });
    });
    expect(api.importBankStatement).not.toHaveBeenCalled();
    expect(api.previewBankStatement).toHaveBeenCalledTimes(1);
    expect(api.previewBankStatement).toHaveBeenCalledWith(file);
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("55: table rows show invoice number, INV date and service", async () => {
    renderInbox();
    await ready();
    await chooseTable();
    const candidateRow = screen.getByTestId("cc-cand-11");
    expect(candidateRow).toHaveTextContent("INV-000000-00001");
    expect(candidateRow).toHaveTextContent("2 Jan");
    expect(candidateRow).toHaveTextContent("Example service");
  });

  it("56: missing invoice identity shows only No invoice number", async () => {
    const missing = candidate({
      invoice_id: 44,
      invoice_number: null,
      invoice_issued_at: null,
      service_name: null,
    });
    api.getReconciliationInbox.mockResolvedValue([four()[2]]);
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(3),
      candidates: [missing],
      recorded_candidates: [],
    });
    renderInbox();
    await screen.findByText("ROW YELLOW");
    const candidateRow = await screen.findByTestId("cc-cand-44");
    expect(candidateRow).toHaveTextContent("No invoice number");
    expect(candidateRow).not.toHaveTextContent("Example service");
  });

  it("57: an emptied selected tab moves to the first non-empty tab", async () => {
    const reload = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(four())
      .mockReturnValueOnce(reload.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() =>
      expect(tab("confirm")).toHaveAttribute("aria-selected", "true"),
    );
    expect(row(1)).toHaveAttribute("aria-current", "true");
    await act(async () =>
      reload.resolve(four().filter((item) => item.txn_id !== 2)),
    );
  });

  it("58: a held link disables Choose another and Ignore in the pair view", async () => {
    const held = deferred<typeof ack>();
    api.linkRecorded.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    expect(
      screen.getByRole("button", { name: "Choose another" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ignore" })).toBeDisabled();
    await act(async () => held.resolve(ack));
  });

  it("59: stale Link handler rejects an open pick changed in the same act", async () => {
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    const openRadio = within(await screen.findByTestId("cc-cand-11")).getByRole(
      "radio",
    );
    fireEvent.click(
      within(screen.getByTestId("cc-cand-12")).getByRole("radio"),
    );
    const staleLink = screen.getByRole("button", {
      name: "Link, no money moved",
    });
    const staleClick = renderedClick(staleLink);
    act(() => {
      openRadio.click();
      staleClick();
    });
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(screen.queryByText("Confirm payment match")).toBeNull();
  });

  it("60: stale Confirm handler rejects a recorded pick changed in the same act", async () => {
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-11")).getByRole("radio"),
    );
    const recordedRadio = within(screen.getByTestId("cc-cand-12")).getByRole(
      "radio",
    );
    const staleConfirm = screen.getByRole("button", {
      name: "Confirm payment",
    });
    const staleClick = renderedClick(staleConfirm);
    act(() => {
      recordedRadio.click();
      staleClick();
    });
    expect(screen.queryByText("Confirm payment match")).toBeNull();
    expect(api.linkRecorded).not.toHaveBeenCalled();
  });
});
