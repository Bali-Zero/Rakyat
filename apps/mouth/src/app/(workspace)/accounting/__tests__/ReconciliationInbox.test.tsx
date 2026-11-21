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
      onOpenOutgoings={vi.fn()}
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

    await openTab("confirm", "ROW GREEN");
    let transfers = within(screen.getByRole("list", { name: "Transfers" }));
    expect(transfers.getAllByRole("listitem")).toHaveLength(1);
    expect(transfers.getByText("ROW GREEN")).toBeInTheDocument();

    await openTab("link", "ROW RECORDED");
    transfers = within(screen.getByRole("list", { name: "Transfers" }));
    expect(transfers.getAllByRole("listitem")).toHaveLength(1);
    expect(transfers.getByText("ROW RECORDED")).toBeInTheDocument();

    await openTab("choose", "ROW YELLOW");
    transfers = within(screen.getByRole("list", { name: "Transfers" }));
    expect(transfers.getAllByRole("listitem")).toHaveLength(2);
    expect(transfers.getByText("ROW YELLOW")).toBeInTheDocument();
    expect(transfers.getByText("ROW ODD")).toBeInTheDocument();

    await openTab("nomatch", "ROW RED");
    transfers = within(screen.getByRole("list", { name: "Transfers" }));
    expect(transfers.getAllByRole("listitem")).toHaveLength(1);
    expect(transfers.getByText("ROW RED")).toBeInTheDocument();
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
    expect(row(1)).not.toHaveAttribute("aria-current");
  });

  it("03: tabs carry counts and filter rows client-side", async () => {
    renderInbox();
    await ready();
    expect(screen.getByTestId("cc-count-confirm")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-link")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-choose")).toHaveTextContent("1");
    expect(screen.getByTestId("cc-count-nomatch")).toHaveTextContent("1");
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
    expect(api.getBankTransactions).toHaveBeenCalledTimes(1);
    expect(api.getBankTransactions).toHaveBeenCalledWith({ limit: 1 });
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    for (const mock of [
      api.linkRecorded,
      api.ignoreTransaction,
      api.reopenTransaction,
      api.confirm,
      api.previewBankStatement,
      api.importBankStatement,
    ]) {
      expect(mock).not.toHaveBeenCalled();
    }
  });

  it("05: To link explains linking and marks the recorded suggestion in table view", async () => {
    renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    expect(
      await screen.findByText("This payment looks already recorded"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Link it instead of confirming it again."),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    ).toBeInTheDocument();
    await chooseTable();
    expect(screen.getByText("Already paid")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("cc-cand-12")).getByText("Suggested"),
    ).toBeInTheDocument();
  });

  it("06: Link sends only on click with the selected ids", async () => {
    const onConfirmed = renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    expect(api.linkRecorded).not.toHaveBeenCalled();
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
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
    const onConfirmed = renderInbox();
    await ready();
    await openTab("link", "ROW RECORDED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("link refused");
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(1);
    expect(row(2)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Link, no money moved" }),
    ).toBeEnabled();
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
    fireEvent.click(button);
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    for (const ignore of screen.getAllByRole("button", { name: "Ignore" })) {
      expect(ignore).toBeDisabled();
    }
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
  });

  it("10: a short ignore reason cannot be sent", async () => {
    renderInbox();
    await ready();
    const input = await openIgnore();
    const send = screen.getByRole("button", { name: "Ignore transfer" });
    expect(send).toBeDisabled();
    fireEvent.change(input, { target: { value: "  ab  " } });
    expect(send).toBeDisabled();
    fireEvent.click(send);
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("11: a valid trimmed ignore reason is sent with the opened id", async () => {
    const onConfirmed = renderInbox();
    await ready();
    const input = await openIgnore();
    fireEvent.change(input, { target: { value: "  not   a payment " } });
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledTimes(1);
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: "not a payment",
    });
    expect(JSON.stringify(toast.success.mock.calls)).not.toContain(
      "not a payment",
    );
    expect(JSON.stringify(toast.error.mock.calls)).not.toContain(
      "not a payment",
    );
  });

  it("12: an ignore error keeps the form and server text", async () => {
    api.ignoreTransaction.mockRejectedValueOnce(new Error("ignore refused"));
    const onConfirmed = renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ignore refused",
    );
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Reason for ignoring")).toHaveValue("abc");
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
    expect(send).toBeDisabled();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
  });

  it("15: linked and ignored loads only after its Done control opens", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [
              {
                ...bank,
                id: 31,
                description: "ROW LINKED",
                reconciled_status: "manual",
              },
            ]
          : options?.reconciledStatus === "ignored"
            ? [
                {
                  ...bank,
                  id: 32,
                  description: "ROW IGNORED",
                  reconciled_status: "ignored",
                },
              ]
            : [],
    );
    renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    expect(api.getBankTransactions).toHaveBeenCalledTimes(1);
    expect(
      api.getBankTransactions.mock.calls
        .map((call) => call[0]?.reconciledStatus)
        .filter(Boolean),
    ).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    expect(await screen.findByText("ROW IGNORED")).toBeInTheDocument();
    await screen.findByText("ROW LINKED");
    await waitFor(() =>
      expect(api.getBankTransactions).toHaveBeenCalledTimes(3),
    );
    const statuses = api.getBankTransactions.mock.calls
      .map((call) => call[0]?.reconciledStatus)
      .filter(Boolean);
    expect(statuses.sort()).toEqual(["ignored", "manual"]);
  });

  it("16: Reopen sends the row id, reloads and reports the write", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [
              {
                ...bank,
                id: 76,
                description: "ROW LINKED",
                reconciled_status: "manual",
              },
            ]
          : options?.reconciledStatus === "ignored"
            ? [
                {
                  ...bank,
                  id: 77,
                  description: "ROW IGNORED",
                  reconciled_status: "ignored",
                },
              ]
            : [],
    );
    const onConfirmed = renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW IGNORED");
    const [reopen] = await screen.findAllByRole("button", { name: "Reopen" });
    expect(api.reopenTransaction).not.toHaveBeenCalled();
    const before = api.getBankTransactions.mock.calls.length;
    fireEvent.click(reopen);
    expect(api.reopenTransaction).toHaveBeenCalledWith(77);
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.reopenTransaction).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(api.getBankTransactions).toHaveBeenCalledTimes(before + 2),
    );
  });

  it("17: pending and failed Reopen stays single and shows its error", async () => {
    api.getBankTransactions.mockImplementation(async (options) =>
      options?.limit === 1
        ? [bank]
        : options?.reconciledStatus === "manual"
          ? [
              {
                ...bank,
                id: 78,
                description: "ROW LINKED",
                reconciled_status: "manual",
              },
            ]
          : options?.reconciledStatus === "ignored"
            ? [
                {
                  ...bank,
                  id: 79,
                  description: "ROW IGNORED",
                  reconciled_status: "ignored",
                },
              ]
            : [],
    );
    const held = deferred<typeof ack>();
    api.reopenTransaction.mockReturnValueOnce(held.promise);
    const onConfirmed = renderInbox();
    await ready();
    fireEvent.click(tab("done"));
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW LINKED");
    const reopen = await screen.findAllByRole("button", { name: "Reopen" });
    fireEvent.click(reopen[0]);
    fireEvent.click(reopen[0]);
    expect(api.reopenTransaction).toHaveBeenCalledTimes(1);
    expect(reopen[0]).toBeDisabled();
    for (const button of reopen) expect(button).toBeDisabled();
    await act(async () => held.reject(new Error("reopen refused")));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent("reopen refused");
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "Reopen" })[0]).toBeEnabled();
  });

  it("18: Confirm payment opens the unchanged dialog and sends the same request", async () => {
    const onConfirmed = renderInbox();
    await ready();
    expect(await screen.findByText("Our invoice")).toBeInTheDocument();
    expect(opened()).toHaveTextContent("INV-000000-00001 · PT CONTOH SATU");
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    expect(api.confirm).not.toHaveBeenCalled();
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledTimes(1);
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
    fireEvent.click(
      screen.getByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 2,
      practice_id: 22,
      invoice_id: 12,
    });
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
        four()[2],
      ])
      .mockReturnValueOnce(reload.promise);
    renderInbox();
    await ready();
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "valid reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId("inbox-row-1")).toBeNull());
    expect(row(5)).toHaveAttribute("aria-current", "true");
    expect(screen.getAllByText("ROW GREEN TWO").length).toBeGreaterThan(0);
    expect(screen.queryByText("Loading inbox…")).toBeNull();
    await openTab("choose", "ROW YELLOW");
    await openTab("confirm", "ROW GREEN TWO");
    await act(async () =>
      reload.resolve([
        inboxItem({ txn_id: 9, light: "green", description: "ROW SERVER" }),
      ]),
    );
    await screen.findAllByText("ROW SERVER");
    expect(screen.queryByText("ROW GREEN TWO")).toBeNull();
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
    await waitFor(() => expect(screen.queryByText("ROW GREEN")).toBeNull());
    await openTab("choose", "ROW YELLOW");
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "valid second reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(3),
    );
    await waitFor(() => expect(screen.queryByText("ROW YELLOW")).toBeNull());
    await act(async () =>
      newer.resolve([
        inboxItem({
          ...four()[1],
          txn_id: 9,
          description: "ROW NEWER",
        }),
      ]),
    );
    expect(screen.getAllByText("ROW NEWER").length).toBeGreaterThan(0);
    await act(async () =>
      old.resolve([
        four()[0],
        inboxItem({
          ...four()[1],
          txn_id: 8,
          description: "ROW OLDER",
        }),
      ]),
    );
    expect(screen.getAllByText("ROW NEWER").length).toBeGreaterThan(0);
    expect(screen.queryByText("ROW GREEN")).toBeNull();
    expect(screen.queryByText("ROW OLDER")).toBeNull();
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
    expect(toast.error).toHaveBeenCalledWith(
      "Failed to load reconciliation inbox",
      "Please try again",
    );
    await openTab("link", "ROW RECORDED");
    await openTab("choose", "ROW YELLOW");
  });

  it("24: a truncated candidate group states both counts", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        ...four()[2],
        open_candidates: 9,
        recorded_candidates: 9,
      }),
    ]);
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(3),
      candidates: [openOne, openTwo],
      recorded_candidates: Array.from({ length: 5 }, (_, index) =>
        candidate({
          invoice_id: 21 + index,
          practice_id: 31 + index,
          client_name: "PT CONTOH SATU",
        }),
      ),
    });
    renderInbox();
    await screen.findByText("ROW YELLOW");
    expect(
      await screen.findByText(
        "9 invoices have this amount; the 2 closest are shown.",
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        "9 invoices have this amount; the 5 closest are shown.",
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
    expect(
      await screen.findByText("Could not load the invoices for this transfer."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Ranking candidates…")).toBeNull();
    api.getMatchCandidates.mockResolvedValueOnce(candidatesFor(1));
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenLastCalledWith(1),
    );
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/PT CONTOH SATU/)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByText("Could not load the invoices for this transfer."),
      ).toBeNull(),
    );
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
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
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
    const onConfirmed = renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "kept reason" } });
    await openTab("link", "ROW RECORDED");
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    api.getReconciliationInbox.mockResolvedValue(
      four().filter((item) => item.txn_id !== 2),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(screen.queryByText("ROW RECORDED")).toBeNull();
    fireEvent.click(tab("confirm"));
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
    const confirm = screen.getByRole("button", { name: /^Confirm paid$/ });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toBeDisabled();
    fireEvent.click(close);
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(api.confirm).toHaveBeenCalledTimes(1);
    expect(
      within(opened()).getByRole("button", { name: "Ignore" }),
    ).toBeDisabled();
    api.getReconciliationInbox.mockResolvedValue(
      four().filter((item) => item.txn_id !== 1),
    );
    await act(async () => held.resolve({} as never));
    await waitFor(() =>
      expect(screen.queryByText("Confirm payment match")).toBeNull(),
    );
    await waitFor(() => expect(screen.queryByText("ROW GREEN")).toBeNull());
    expect(api.confirm).toHaveBeenCalledTimes(1);
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
    fireEvent.click(openRadio);
    const confirmClick = renderedClick(
      screen.getByRole("button", { name: "Confirm payment" }),
    );
    fireEvent.click(recordedRadio);
    const link = screen.getByRole("button", { name: "Link, no money moved" });
    act(() => {
      link.click();
      openRadio.click();
      confirmClick();
    });
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Confirm payment match")).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();
    api.getReconciliationInbox.mockResolvedValue(
      four().filter((item) => item.txn_id !== 3),
    );
    await act(async () => held.resolve(ack));
    await waitFor(() => expect(screen.queryByText("ROW YELLOW")).toBeNull());
    expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2);
    expect(api.confirm).not.toHaveBeenCalled();
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
    fireEvent.click(recordedRadio);
    const linkClick = renderedClick(
      screen.getByRole("button", { name: "Link, no money moved" }),
    );
    fireEvent.click(openRadio);
    const confirm = screen.getByRole("button", { name: "Confirm payment" });
    act(() => {
      confirm.click();
      recordedRadio.click();
      linkClick();
    });
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(api.linkRecorded).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(screen.queryByText("Confirm payment match")).toBeNull(),
    );
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
      await act(async () => held.resolve({} as never));
      await waitFor(() =>
        expect(screen.queryByText("Confirm payment match")).toBeNull(),
      );
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
    expect(screen.queryByText("Confirm payment match")).toBeNull();
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
    expect(screen.getByRole("button", { name: "Ignore" })).toBeEnabled();
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
    fireEvent.click(importButton);
    expect(api.importBankStatement).not.toHaveBeenCalled();
    await act(async () => held.resolve(ack));
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);
    await waitFor(() =>
      expect(api.importBankStatement).toHaveBeenCalledTimes(1),
    );
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
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    await act(async () => held.resolve(ack));
    await waitFor(() =>
      expect(api.getReconciliationInbox).toHaveBeenCalledTimes(2),
    );
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
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
    for (const [memo, expected] of [
      ["", ""],
      ["MEMO CONTOH", "MEMO CONTOH"],
    ] as const) {
      api.confirm.mockClear();
      api.getReconciliationInbox.mockResolvedValue([
        inboxItem({ ...four()[0], description: memo }),
      ]);
      const { unmount } = render(
        <ReconciliationInbox
          onConfirmed={vi.fn()}
          doneVersion={0}
          onOpenCashout={vi.fn()}
          onOpenOutgoings={vi.fn()}
        />,
      );
      await screen.findByText(memo || "(no memo)");
      fireEvent.click(
        await screen.findByRole("button", { name: "Confirm payment" }),
      );
      fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
      await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
      expect(api.confirm.mock.calls[0][0].payment_reference).toBe(expected);
      unmount();
    }
  });

  it("44: ignore counts code points after collapsing whitespace", async () => {
    const reasonAtLimit = "😀".repeat(200);
    const atLimit = render(
      <ReconciliationInbox
        onConfirmed={vi.fn()}
        doneVersion={0}
        onOpenCashout={vi.fn()}
        onOpenOutgoings={vi.fn()}
      />,
    );
    await ready();
    const atLimitInput = await openIgnore();
    const atLimitSend = screen.getByRole("button", {
      name: "Ignore transfer",
    });
    fireEvent.change(atLimitInput, { target: { value: "😀😀" } });
    expect(atLimitSend).toBeDisabled();
    fireEvent.change(atLimitInput, { target: { value: "😀😀😀" } });
    expect(atLimitSend).toBeEnabled();
    fireEvent.change(atLimitInput, { target: { value: reasonAtLimit } });
    expect(atLimitSend).toBeEnabled();
    fireEvent.click(atLimitSend);
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: reasonAtLimit,
    });
    atLimit.unmount();

    api.ignoreTransaction.mockClear();
    const overLimit = render(
      <ReconciliationInbox
        onConfirmed={vi.fn()}
        doneVersion={0}
        onOpenCashout={vi.fn()}
        onOpenOutgoings={vi.fn()}
      />,
    );
    await ready();
    const overLimitInput = await openIgnore();
    const overLimitSend = screen.getByRole("button", {
      name: "Ignore transfer",
    });
    fireEvent.change(overLimitInput, {
      target: { value: "😀".repeat(201) },
    });
    expect(overLimitSend).toBeDisabled();
    fireEvent.click(overLimitSend);
    expect(api.ignoreTransaction).not.toHaveBeenCalled();
    overLimit.unmount();

    for (const [reason, expected] of [
      ["  🧾   ok ", "🧾 ok"],
      ["  a  \t b ", "a b"],
    ] as const) {
      api.ignoreTransaction.mockClear();
      const { unmount } = render(
        <ReconciliationInbox
          onConfirmed={vi.fn()}
          doneVersion={0}
          onOpenCashout={vi.fn()}
          onOpenOutgoings={vi.fn()}
        />,
      );
      await ready();
      const input = await openIgnore();
      const send = screen.getByRole("button", { name: "Ignore transfer" });
      fireEvent.change(input, { target: { value: reason } });
      expect(send).toBeEnabled();
      fireEvent.click(send);
      await waitFor(() =>
        expect(api.ignoreTransaction).toHaveBeenCalledTimes(1),
      );
      expect(api.ignoreTransaction).toHaveBeenCalledWith({
        bank_txn_id: 1,
        reason: expected,
      });
      unmount();
    }
  });

  it("45: a failed first load retries into the calm view", async () => {
    for (const hasBankData of [true, false]) {
      api.getReconciliationInbox
        .mockReset()
        .mockRejectedValueOnce(new Error("first failed"))
        .mockResolvedValueOnce([]);
      api.getBankTransactions.mockResolvedValue(hasBankData ? [bank] : []);
      const { unmount } = render(
        <ReconciliationInbox
          onConfirmed={vi.fn()}
          doneVersion={0}
          onOpenCashout={vi.fn()}
          onOpenOutgoings={vi.fn()}
        />,
      );
      expect(
        await screen.findByText("Could not load the inbox."),
      ).toBeInTheDocument();
      expect(screen.queryByText("All done for today")).toBeNull();
      fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
      if (hasBankData) {
        expect(await screen.findByTestId("cc-calm")).toHaveTextContent(
          "All done for today",
        );
      } else {
        expect(await screen.findByTestId("cc-dayone")).toBeInTheDocument();
        expect(screen.queryByTestId("cc-calm")).toBeNull();
      }
      expect(screen.queryByText("Could not load the inbox.")).toBeNull();
      unmount();
    }
  });

  it("46: a newer failed load drops an older held answer", async () => {
    const held = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockReturnValueOnce(held.promise)
      .mockRejectedValueOnce(new Error("failed"));
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockResolvedValue(importFixture());
    renderInbox();
    expect(screen.getByText("Loading inbox…")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    expect(
      await screen.findByText("Could not load the inbox."),
    ).toBeInTheDocument();
    await act(async () => held.resolve([]));
    expect(screen.getByText("Could not load the inbox.")).toBeInTheDocument();
    expect(screen.queryByText("All done for today")).toBeNull();
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
    await openTab("choose", "ROW YELLOW");
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
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
    expect(screen.queryByText("All done for today")).toBeNull();
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
    await act(async () => held.resolve([]));
    expect(await screen.findByTestId("cc-calm")).toHaveTextContent(
      "All done for today",
    );
  });

  it("49: when the last-row reload fails, the failure view wins", async () => {
    const reload = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0]])
      .mockReturnValueOnce(reload.promise);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByText("Loading inbox…")).toBeInTheDocument();
    await act(async () => reload.reject(new Error("reload failed")));
    expect(
      await screen.findByText("Could not load the inbox."),
    ).toBeInTheDocument();
  });

  it("50: a new last-row reload replaces an older failure with loading", async () => {
    const held = deferred<ReturnType<typeof four>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce([four()[0], four()[2]])
      .mockRejectedValueOnce(new Error("reload failed"))
      .mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    await openTab("confirm", "ROW GREEN");
    fireEvent.change(await openIgnore(), { target: { value: "valid reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    expect(await screen.findByText("Loading inbox…")).toBeInTheDocument();
    expect(screen.queryByText("Could not load the inbox.")).toBeNull();
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
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 1,
      reason: "latest reason",
    });
    expect(api.ignoreTransaction).toHaveBeenCalledTimes(1);
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
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(1),
      recorded_candidates: [
        candidate({
          ...recorded,
          invoice_issued_at: "2026-01-03T00:00:00Z",
          service_name: "Example recorded service",
        }),
      ],
    });
    renderInbox();
    await ready();
    await chooseTable();
    const candidateRow = screen.getByTestId("cc-cand-11");
    expect(candidateRow).toHaveTextContent("INV-000000-00001");
    expect(candidateRow).toHaveTextContent("2 Jan");
    expect(candidateRow).toHaveTextContent("Example service");
    expect(within(candidateRow).getByText("2 Jan")).toHaveTextContent(
      /^2 Jan$/,
    );
    const recordedRow = screen.getByTestId("cc-cand-12");
    expect(recordedRow).toHaveTextContent("INV-000000-00002");
    expect(recordedRow).toHaveTextContent("3 Jan");
    expect(recordedRow).toHaveTextContent("Example recorded service");
    expect(within(recordedRow).getByText("3 Jan")).toHaveTextContent(/^3 Jan$/);
  });

  it("56: missing invoice identity shows only No invoice number", async () => {
    const missing = candidate({
      invoice_id: 44,
      invoice_number: null,
      invoice_issued_at: null,
      service_name: null,
    });
    const absent = candidate({ invoice_id: 13, practice_id: 23 });
    api.getReconciliationInbox.mockResolvedValue([four()[2]]);
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(3),
      candidates: [missing, absent],
      recorded_candidates: [],
    });
    renderInbox();
    await screen.findByText("ROW YELLOW");
    const candidateRow = await screen.findByTestId("cc-cand-44");
    expect(candidateRow).toHaveTextContent("No invoice number");
    expect(candidateRow).not.toHaveTextContent("Example service");
    const absentRow = await screen.findByTestId("cc-cand-13");
    expect(
      [candidateRow, absentRow].map((candidate) =>
        Array.from(candidate.children)
          .slice(1, 4)
          .map((identity) => identity.textContent),
      ),
    ).toEqual([
      ["No invoice number", "", ""],
      ["No invoice number", "", ""],
    ]);
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

  it("61: a held import disables Skip for now and Ignore in the To choose table view", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      ...four(),
      inboxItem({
        txn_id: 5,
        light: "yellow",
        description: "ROW YELLOW TWO",
        open_candidates: 1,
      }),
    ]);
    const held = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    await screen.findByTestId("cc-cand-11");
    const skip = screen.getByRole("button", { name: "Skip for now" });
    const ignore = screen.getByRole("button", { name: "Ignore" });
    expect(skip).toBeEnabled();
    expect(ignore).toBeEnabled();
    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    expect(skip).toBeDisabled();
    expect(ignore).toBeDisabled();
    fireEvent.click(skip);
    fireEvent.click(ignore);
    expect(row(3)).toHaveAttribute("aria-current", "true");
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    await act(async () => held.resolve(importFixture()));
    await waitFor(() => {
      expect(skip).toBeEnabled();
      expect(ignore).toBeEnabled();
    });
  });

  it("62: a Confirm handler of an earlier transfer rejects the pick of the one opened after it", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 101,
        light: "green",
        description: "ROW CONFIRM A",
        open_candidates: 1,
        suggestion: openOne,
      }),
      inboxItem({
        txn_id: 102,
        light: "green",
        description: "ROW CONFIRM B",
        open_candidates: 1,
        suggestion: openTwo,
      }),
    ]);
    api.getMatchCandidates.mockImplementation(async (txnId) => ({
      txn_id: txnId,
      transfer_amount_idr: 500000,
      candidates: [txnId === 101 ? openOne : openTwo],
      recorded_candidates: [],
    }));
    renderInbox();
    await screen.findByText("ROW CONFIRM A");
    await within(opened()).findByText("INV-000000-00001", { exact: false });
    const staleClick = renderedClick(
      screen.getByRole("button", { name: "Confirm payment" }),
    );

    fireEvent.click(row(102));
    await within(opened()).findByText("INV-000000-00003", { exact: false });
    await act(async () => staleClick());

    expect(screen.queryByText("Confirm payment match")).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("63: a Link handler of an earlier transfer rejects the pick of the one opened after it", async () => {
    const recordedTwo = candidate({
      invoice_id: 14,
      practice_id: 24,
      client_name: "PT CONTOH EMPAT",
      invoice_number: "INV-000000-00004",
      reasons: ["payment already recorded"],
    });
    api.getReconciliationInbox.mockResolvedValue([
      inboxItem({
        txn_id: 201,
        light: "recorded",
        description: "ROW LINK A",
        recorded_candidates: 1,
        suggestion: recorded,
      }),
      inboxItem({
        txn_id: 202,
        light: "recorded",
        description: "ROW LINK B",
        recorded_candidates: 1,
        suggestion: recordedTwo,
      }),
    ]);
    api.getMatchCandidates.mockImplementation(async (txnId) => ({
      txn_id: txnId,
      transfer_amount_idr: 500000,
      candidates: [],
      recorded_candidates: [txnId === 201 ? recorded : recordedTwo],
    }));
    renderInbox();
    await screen.findByText("ROW LINK A");
    await within(opened()).findByText("INV-000000-00002", { exact: false });
    const staleClick = renderedClick(
      screen.getByRole("button", { name: "Link, no money moved" }),
    );

    fireEvent.click(row(202));
    await within(opened()).findByText("INV-000000-00004", { exact: false });
    await act(async () => staleClick());

    expect(api.linkRecorded).not.toHaveBeenCalled();
  });

  it("64: confirming a hand-picked invoice sends that invoice", async () => {
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-13")).getByRole("radio"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm payment" }));
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(api.confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({
      bank_txn_id: 3,
      practice_id: 23,
      invoice_id: 13,
      amount_applied_idr: 500000,
      new_status: "paid",
      payment_method: "bank_transfer",
      payment_reference: "ROW YELLOW",
      movement_date: "2026-01-05",
      pph_withheld_idr: 0,
    });
  });

  it("65: linking a hand-picked invoice sends that invoice", async () => {
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(1),
      recorded_candidates: [recorded],
    });
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-12")).getByRole("radio"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Link, no money moved" }),
    );
    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 1,
      practice_id: 22,
      invoice_id: 12,
    });
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("66: a stale Ignore transfer handler rejects a form moved to another transfer", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      four()[0],
      inboxItem({ ...four()[0], txn_id: 5, description: "ROW GREEN TWO" }),
    ]);
    renderInbox();
    await ready();
    fireEvent.change(await openIgnore(), { target: { value: "reason A" } });
    const staleClick = renderedClick(
      screen.getByRole("button", { name: "Ignore transfer" }),
    );

    fireEvent.click(row(5));
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "reason B" },
    });
    await act(async () => staleClick());

    expect(api.ignoreTransaction).not.toHaveBeenCalled();
  });

  it("67: a held import disables the picked TABLE VIEW primary", async () => {
    const held = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("choose", "ROW YELLOW");
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-11")).getByRole("radio"),
    );
    const primary = screen.getByRole("button", { name: "Confirm payment" });
    expect(primary).toBeEnabled();

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    await waitFor(() => {
      expect(api.importBankStatement).toHaveBeenCalledTimes(1);
      expect(primary).toBeDisabled();
    });

    await act(async () => held.resolve(importFixture()));
    await waitFor(() => expect(primary).toBeEnabled());
  });

  it("68: a held import disables both NOTHING VIEW footer controls", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      four()[3],
      inboxItem({ txn_id: 6, light: "red", description: "ROW RED TWO" }),
    ]);
    const held = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockReturnValueOnce(held.promise);
    renderInbox();
    await screen.findByText("ROW RED");
    await screen.findByText("Nothing found");
    const skip = screen.getByRole("button", { name: "Skip for now" });
    const ignore = screen.getByRole("button", {
      name: "Ignore with a reason",
    });
    expect(skip).toBeEnabled();
    expect(ignore).toBeEnabled();

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    await waitFor(() => {
      expect(api.importBankStatement).toHaveBeenCalledTimes(1);
      expect(skip).toBeDisabled();
      expect(ignore).toBeDisabled();
    });

    await act(async () => held.resolve(importFixture()));
    await waitFor(() => {
      expect(skip).toBeEnabled();
      expect(ignore).toBeEnabled();
    });
  });

  it("69: a held import disables the ignore form reason and Cancel", async () => {
    const held = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockResolvedValue(previewFixture());
    api.importBankStatement.mockReturnValueOnce(held.promise);
    renderInbox();
    await ready();
    await openTab("nomatch", "ROW RED");
    fireEvent.click(
      await screen.findByRole("button", { name: "Ignore with a reason" }),
    );
    const reason = screen.getByLabelText("Reason for ignoring");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(reason).toBeEnabled();
    expect(cancel).toBeEnabled();

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    await waitFor(() => {
      expect(api.importBankStatement).toHaveBeenCalledTimes(1);
      expect(reason).toBeDisabled();
      expect(cancel).toBeDisabled();
    });

    await act(async () => held.resolve(importFixture()));
    await waitFor(() => {
      expect(reason).toBeEnabled();
      expect(cancel).toBeEnabled();
    });
  });

  it("70: confirming a hand-picked invoice overrides the suggested invoice", async () => {
    api.getMatchCandidates.mockResolvedValue({
      ...candidatesFor(1),
      candidates: [openOne, openTwo],
    });
    renderInbox();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-13")).getByRole("radio"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm payment" }));
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    expect(api.confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));

    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({
      bank_txn_id: 1,
      practice_id: 23,
      invoice_id: 13,
      amount_applied_idr: 500000,
      new_status: "paid",
      payment_method: "bank_transfer",
      payment_reference: "ROW GREEN",
      movement_date: "2026-01-05",
      pph_withheld_idr: 0,
    });
  });
});

describe("the transfer acted on (TEST-SPEC T2, T3)", () => {
  const transferAmount = (txnId: number) => 1_000_000 + txnId;

  const candidatesForTransfer = (txnId: number) => {
    const makeCandidate = (slot: 1 | 2 | 3) => {
      const identity = txnId * 10 + slot;
      return candidate({
        invoice_id: 10_000 + identity,
        practice_id: 20_000 + identity,
        client_name: `ACCOUNT ${identity}`,
        invoice_number: `TEST-${identity}`,
        invoice_issued_at: `2026-03-${String(slot).padStart(2, "0")}T00:00:00Z`,
        service_name: `SERVICE ${identity}`,
        invoice_amount_idr: 500_000 + identity,
        reasons: [`fixture reason ${identity}`],
      });
    };
    const firstOpen = makeCandidate(1);
    const suggestedOpen = makeCandidate(2);
    const recordedCandidate = makeCandidate(3);
    return {
      txn_id: txnId,
      transfer_amount_idr: transferAmount(txnId),
      candidates: [firstOpen, suggestedOpen],
      recorded_candidates: [recordedCandidate],
    };
  };

  const transfer = (
    txnId: number,
    light: "green" | "recorded" | "yellow" | "red",
  ) => {
    const answer = candidatesForTransfer(txnId);
    return inboxItem({
      txn_id: txnId,
      txn_date: `2026-02-${String(txnId - 100).padStart(2, "0")}`,
      amount_idr: transferAmount(txnId),
      description: `TRANSFER ${txnId}`,
      light,
      open_candidates: 2,
      recorded_candidates: 1,
      suggestion: answer.candidates[1],
    });
  };

  const twelve = () => [
    transfer(101, "green"),
    transfer(102, "green"),
    transfer(103, "green"),
    transfer(104, "recorded"),
    transfer(105, "recorded"),
    transfer(106, "recorded"),
    transfer(107, "yellow"),
    transfer(108, "yellow"),
    transfer(109, "yellow"),
    transfer(110, "red"),
    transfer(111, "red"),
    transfer(112, "red"),
  ];

  it("71: confirms the third transfer with its complete identity and after-effect", async () => {
    const initial = twelve();
    const reload = deferred<ReturnType<typeof twelve>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(reload.promise);
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    const onConfirmed = renderInbox();
    await screen.findByText("TRANSFER 101");

    fireEvent.click(row(103));
    await waitFor(() =>
      expect(row(103)).toHaveAttribute("aria-current", "true"),
    );
    await within(opened()).findByText("TEST-1032", { exact: false });
    expect(opened()).toHaveTextContent("Rp1.000.103");
    expect(opened()).toHaveTextContent("TRANSFER 103");
    expect(opened()).toHaveTextContent("Received Tue 3 Feb 2026");

    fireEvent.click(
      within(opened()).getByRole("button", { name: "Confirm payment" }),
    );
    const dialog = screen
      .getByText("Confirm payment match")
      .closest("div.fixed");
    expect(dialog).not.toBeNull();
    expect(dialog).toHaveTextContent("Rp 1.000.103");
    expect(dialog).toHaveTextContent("TEST-1032");
    expect(dialog).toHaveTextContent("ACCOUNT 1032");
    expect(api.confirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({
      bank_txn_id: 103,
      practice_id: 21_032,
      invoice_id: 11_032,
      amount_applied_idr: 1_000_103,
      new_status: "paid",
      payment_method: "bank_transfer",
      payment_reference: "TRANSFER 103",
      movement_date: "2026-02-03",
      pph_withheld_idr: 0,
    });
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.queryByTestId("inbox-row-103")).toBeNull(),
    );
    expect(row(101)).toBeInTheDocument();
    expect(row(102)).toBeInTheDocument();
    expect(row(101)).toHaveAttribute("aria-current", "true");
    expect(opened()).toHaveTextContent("TRANSFER 101");
    expect(screen.getByTestId("cc-count-confirm")).toHaveTextContent("2");
    expect(onConfirmed).toHaveBeenCalledTimes(1);
  });

  it("72: links the second transfer with its exact ids and after-effect", async () => {
    const initial = twelve();
    const reload = deferred<ReturnType<typeof twelve>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(reload.promise);
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    const onConfirmed = renderInbox();
    await screen.findByText("TRANSFER 101");

    await openTab("link", "TRANSFER 104");
    fireEvent.click(row(105));
    await waitFor(() =>
      expect(row(105)).toHaveAttribute("aria-current", "true"),
    );
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-11053")).getByRole("radio"),
    );
    fireEvent.click(
      within(opened()).getByRole("button", {
        name: "Link, no money moved",
      }),
    );

    await waitFor(() => expect(api.linkRecorded).toHaveBeenCalledTimes(1));
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 105,
      practice_id: 21_053,
      invoice_id: 11_053,
    });
    expect(api.confirm).not.toHaveBeenCalled();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.queryByTestId("inbox-row-105")).toBeNull(),
    );
    expect(row(104)).toBeInTheDocument();
    expect(row(106)).toBeInTheDocument();
    expect(row(104)).toHaveAttribute("aria-current", "true");
    expect(opened()).toHaveTextContent("TRANSFER 104");
    expect(screen.getByTestId("cc-count-link")).toHaveTextContent("2");
    expect(onConfirmed).toHaveBeenCalledTimes(1);
  });

  it("73: ignores the third transfer with its exact id and after-effect", async () => {
    const initial = twelve();
    const reload = deferred<ReturnType<typeof twelve>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(reload.promise);
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    const onConfirmed = renderInbox();
    await screen.findByText("TRANSFER 101");

    await openTab("nomatch", "TRANSFER 110");
    fireEvent.click(row(112));
    await waitFor(() =>
      expect(row(112)).toHaveAttribute("aria-current", "true"),
    );
    await screen.findByTestId("cc-cand-11121");
    fireEvent.click(within(opened()).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "duplicate test transfer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));

    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    expect(api.ignoreTransaction).toHaveBeenCalledWith({
      bank_txn_id: 112,
      reason: "duplicate test transfer",
    });
    expect(api.confirm).not.toHaveBeenCalled();
    expect(api.linkRecorded).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.queryByTestId("inbox-row-112")).toBeNull(),
    );
    expect(row(110)).toBeInTheDocument();
    expect(row(111)).toBeInTheDocument();
    expect(row(110)).toHaveAttribute("aria-current", "true");
    expect(opened()).toHaveTextContent("TRANSFER 110");
    expect(screen.getByTestId("cc-count-nomatch")).toHaveTextContent("2");
    expect(screen.queryByTestId("cc-ignore-form")).toBeNull();
    expect(onConfirmed).toHaveBeenCalledTimes(1);
  });

  it("74: keeps a failed link on the second transfer only", async () => {
    api.getReconciliationInbox.mockResolvedValueOnce(twelve());
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    api.linkRecorded.mockRejectedValueOnce(new Error("link target failed"));
    renderInbox();
    await screen.findByText("TRANSFER 101");

    await openTab("link", "TRANSFER 104");
    fireEvent.click(row(105));
    fireEvent.click(
      within(await screen.findByTestId("cc-cand-11053")).getByRole("radio"),
    );
    fireEvent.click(
      within(opened()).getByRole("button", {
        name: "Link, no money moved",
      }),
    );

    expect(await within(opened()).findByRole("alert")).toHaveTextContent(
      "link target failed",
    );
    expect(row(105)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(opened()).getByRole("button", {
          name: "Link, no money moved",
        }),
      ).toBeEnabled(),
    );
    expect(api.linkRecorded).toHaveBeenCalledTimes(1);
    expect(api.linkRecorded).toHaveBeenCalledWith({
      bank_txn_id: 105,
      practice_id: 21_053,
      invoice_id: 11_053,
    });

    fireEvent.click(row(104));
    await screen.findByTestId("cc-cand-11043");
    expect(within(opened()).queryByRole("alert")).toBeNull();
    expect(opened()).not.toHaveTextContent("link target failed");
  });

  it("75: Skip for now advances through three rows and wraps", async () => {
    api.getReconciliationInbox.mockResolvedValueOnce(twelve());
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    renderInbox();
    await screen.findByText("TRANSFER 101");
    await openTab("choose", "TRANSFER 107");
    await screen.findByTestId("cc-cand-11071");
    api.getMatchCandidates.mockClear();

    fireEvent.click(
      within(opened()).getByRole("button", { name: "Skip for now" }),
    );
    await screen.findByTestId("cc-cand-11081");
    expect(row(108)).toHaveAttribute("aria-current", "true");
    expect(row(109)).not.toHaveAttribute("aria-current");
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(1);
    expect(api.getMatchCandidates).toHaveBeenLastCalledWith(108);
    api.getMatchCandidates.mockClear();

    fireEvent.click(
      within(opened()).getByRole("button", { name: "Skip for now" }),
    );
    await screen.findByTestId("cc-cand-11091");
    expect(row(109)).toHaveAttribute("aria-current", "true");
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(1);
    expect(api.getMatchCandidates).toHaveBeenLastCalledWith(109);
    api.getMatchCandidates.mockClear();

    fireEvent.click(
      within(opened()).getByRole("button", { name: "Skip for now" }),
    );
    await screen.findByTestId("cc-cand-11071");
    expect(row(107)).toHaveAttribute("aria-current", "true");
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(1);
    expect(api.getMatchCandidates).toHaveBeenLastCalledWith(107);
  });

  it("76: Try again reloads candidates for the failed non-first transfer", async () => {
    api.getReconciliationInbox.mockResolvedValueOnce(twelve());
    let targetAttempts = 0;
    api.getMatchCandidates.mockImplementation(async (txnId) => {
      if (txnId === 108 && targetAttempts++ === 0)
        throw new Error("candidate target failed");
      return candidatesForTransfer(txnId);
    });
    renderInbox();
    await screen.findByText("TRANSFER 101");

    await openTab("choose", "TRANSFER 107");
    await screen.findByTestId("cc-cand-11071");
    fireEvent.click(row(108));
    expect(
      await within(opened()).findByText(
        "Could not load the invoices for this transfer.",
      ),
    ).toBeInTheDocument();
    expect(row(108)).toHaveAttribute("aria-current", "true");
    api.getMatchCandidates.mockClear();

    fireEvent.click(
      within(opened()).getByRole("button", { name: "Try again" }),
    );
    const targetCandidate = await screen.findByTestId("cc-cand-11081");
    expect(targetCandidate).toHaveTextContent("TEST-1081");
    expect(opened()).toHaveTextContent("TRANSFER 108");
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(1);
    expect(api.getMatchCandidates).toHaveBeenLastCalledWith(108);
  });

  it("77: confirming keeps the dialog transfer across a tab switch", async () => {
    const initial = twelve();
    const reload = deferred<ReturnType<typeof twelve>>();
    api.getReconciliationInbox
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(reload.promise);
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    const onConfirmed = renderInbox();
    await screen.findByText("TRANSFER 101");

    fireEvent.click(row(103));
    await within(opened()).findByText("TEST-1032", { exact: false });
    fireEvent.click(
      within(opened()).getByRole("button", { name: "Confirm payment" }),
    );
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();

    fireEvent.click(tab("link"));
    await waitFor(() =>
      expect(tab("link")).toHaveAttribute("aria-selected", "true"),
    );
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Confirm paid$/ }));

    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith({
      bank_txn_id: 103,
      practice_id: 21_032,
      invoice_id: 11_032,
      amount_applied_idr: 1_000_103,
      new_status: "paid",
      payment_method: "bank_transfer",
      payment_reference: "TRANSFER 103",
      movement_date: "2026-02-03",
      pph_withheld_idr: 0,
    });
    expect(api.linkRecorded).not.toHaveBeenCalled();
    expect(api.ignoreTransaction).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.getByTestId("cc-count-confirm")).toHaveTextContent("2"),
    );
    expect(screen.getByTestId("cc-count-link")).toHaveTextContent("3");
    const transfers = within(screen.getByRole("list", { name: "Transfers" }));
    expect(transfers.getAllByRole("listitem")).toHaveLength(3);
    expect(row(104)).toBeInTheDocument();
    expect(row(105)).toBeInTheDocument();
    expect(row(106)).toBeInTheDocument();
    expect(onConfirmed).toHaveBeenCalledTimes(1);
  });

  it("78: the ignore form and hidden footer stay with the second transfer", async () => {
    api.getReconciliationInbox.mockResolvedValueOnce(twelve());
    api.getMatchCandidates.mockImplementation(async (txnId) =>
      candidatesForTransfer(txnId),
    );
    renderInbox();
    await screen.findByText("TRANSFER 101");

    await openTab("choose", "TRANSFER 107");
    await screen.findByTestId("cc-cand-11071");
    fireEvent.click(row(108));
    await screen.findByTestId("cc-cand-11081");
    fireEvent.click(within(opened()).getByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "second transfer reason" },
    });

    expect(within(opened()).getByTestId("cc-ignore-form")).toBeInTheDocument();
    expect(within(opened()).queryByTestId("cc-foot")).toBeNull();
    expect(
      within(opened()).queryByRole("button", { name: "Confirm payment" }),
    ).toBeNull();
    expect(
      within(opened()).queryByRole("button", { name: "Skip for now" }),
    ).toBeNull();
    expect(
      within(opened()).queryByRole("button", { name: /^Ignore$/ }),
    ).toBeNull();

    fireEvent.click(row(107));
    await screen.findByTestId("cc-cand-11071");
    expect(within(opened()).queryByTestId("cc-ignore-form")).toBeNull();
    expect(within(opened()).getByTestId("cc-foot")).toBeInTheDocument();
    expect(
      within(opened()).getByRole("button", { name: "Confirm payment" }),
    ).toBeInTheDocument();
    expect(
      within(opened()).getByRole("button", { name: "Skip for now" }),
    ).toBeInTheDocument();
    expect(
      within(opened()).getByRole("button", { name: /^Ignore$/ }),
    ).toBeInTheDocument();

    fireEvent.click(row(108));
    const restoredForm = await within(opened()).findByTestId("cc-ignore-form");
    expect(
      within(restoredForm).getByLabelText("Reason for ignoring"),
    ).toHaveValue("second transfer reason");
    expect(within(opened()).queryByTestId("cc-foot")).toBeNull();
  });

  it("79: positive item counts keep empty No match answers in table view", async () => {
    api.getReconciliationInbox.mockResolvedValueOnce([
      inboxItem({
        txn_id: 201,
        light: "red",
        description: "OPEN COUNT TRANSFER",
        open_candidates: 2,
        recorded_candidates: 0,
      }),
      inboxItem({
        txn_id: 202,
        light: "red",
        description: "RECORDED COUNT TRANSFER",
        open_candidates: 0,
        recorded_candidates: 1,
      }),
    ]);
    api.getMatchCandidates.mockImplementation(async (txnId) => ({
      txn_id: txnId,
      transfer_amount_idr: transferAmount(txnId),
      candidates: [],
      recorded_candidates: [],
    }));
    renderInbox();

    await screen.findByText("OPEN COUNT TRANSFER");
    await within(opened()).findByText("Open invoices");
    expect(opened()).not.toHaveTextContent("Nothing found");
    expect(opened()).toHaveTextContent(
      "2 invoices have this amount; the 0 closest are shown.",
    );

    fireEvent.click(row(202));
    await within(opened()).findByText("Already paid");
    expect(opened()).not.toHaveTextContent("Nothing found");
    expect(opened()).toHaveTextContent(
      "1 invoices have this amount; the 0 closest are shown.",
    );
  });

  it("80: Suggested stays in the matching candidate group and situation tab", async () => {
    const ids = [101, 104, 107, 110] as const;
    const lights = ["green", "recorded", "yellow", "red"] as const;
    const suggestions = new Map<
      number,
      ReturnType<typeof candidatesForTransfer>["candidates"][number]
    >(ids.map((txnId) => [txnId, candidatesForTransfer(txnId).candidates[1]]));
    api.getReconciliationInbox.mockResolvedValueOnce(
      ids.map((txnId, index) => ({
        ...transfer(txnId, lights[index]),
        open_candidates: 1,
        recorded_candidates: 1,
        suggestion: suggestions.get(txnId)!,
      })),
    );
    api.getMatchCandidates.mockImplementation(async (txnId) => {
      const suggested = suggestions.get(txnId)!;
      return {
        txn_id: txnId,
        transfer_amount_idr: transferAmount(txnId),
        candidates: [suggested],
        recorded_candidates: [suggested],
      };
    });
    renderInbox();
    await screen.findByText("TRANSFER 101");

    const suggestedIn = (heading: "Open invoices" | "Already paid") => {
      const group = within(opened())
        .getByRole("heading", { name: heading })
        .closest("div");
      if (!group) throw new Error(`Missing ${heading} group`);
      return within(group).queryAllByText("Suggested");
    };

    fireEvent.click(
      await within(opened()).findByRole("button", { name: "Choose another" }),
    );
    expect(suggestedIn("Open invoices")).toHaveLength(1);
    expect(suggestedIn("Already paid")).toHaveLength(0);

    await openTab("link", "TRANSFER 104");
    fireEvent.click(
      await within(opened()).findByRole("button", { name: "Choose another" }),
    );
    expect(suggestedIn("Open invoices")).toHaveLength(0);
    expect(suggestedIn("Already paid")).toHaveLength(1);

    await openTab("choose", "TRANSFER 107");
    await within(opened()).findByRole("heading", { name: "Open invoices" });
    expect(within(opened()).queryAllByText("Suggested")).toHaveLength(0);

    await openTab("nomatch", "TRANSFER 110");
    await within(opened()).findByRole("heading", { name: "Open invoices" });
    expect(within(opened()).queryAllByText("Suggested")).toHaveLength(0);
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
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm payment" }),
    );
    expect(screen.getByText("Confirm payment match")).toBeInTheDocument();
  };

  it("is off by default, and the dialog says nothing about emails (innocence)", async () => {
    renderInbox();
    expect(await screen.findByText("ROW GREEN")).toBeInTheDocument();
    expect(toggle()).not.toBeChecked();
    await openDialog();
    expect(screen.queryByText("Emails", { selector: "dt" })).toBeNull();
    expect(screen.queryByText(/Historical mode: no email goes/)).toBeNull();
  });

  it("on: the dialog says no emails and Confirm sends historical: true with the same request", async () => {
    const onConfirmed = renderInbox();
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
    const onConfirmed = renderInbox();
    expect(await screen.findByText("ROW GREEN")).toBeInTheDocument();
    await openDialog();
    fireEvent.click(toggle());
    expect(screen.queryByText("Emails", { selector: "dt" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm paid" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith(ORDINARY);
  });
});
