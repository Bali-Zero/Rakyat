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
import { CcAmount } from "../_components/CcAmount";
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
  invoice_amount_idr: 12400000,
});
const otherYearCandidate = candidate({
  invoice_id: 13,
  practice_id: 23,
  client_name: "PT CONTOH TIGA",
  invoice_number: "INV-000000-00003",
  invoice_issued_at: "2025-11-06T00:00:00Z",
  service_name: "Example older service",
  reasons: ["different year"],
  invoice_amount_idr: 11800000,
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
    payer_name: "CONTOH PEMBAYAR",
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
  items: [
    {
      txn_id: 101,
      action: "confirmed" as const,
      done_at: "2026-11-18T08:01:00+08:00",
      txn_date: "2026-11-18",
      amount_idr: 100000,
      description: "DONE CONTOH ONE",
      invoice_number: "INV-000000-00001",
      client_name: "PT CONTOH SATU",
    },
    {
      txn_id: 102,
      action: "confirmed" as const,
      done_at: "2026-11-18T08:02:00+08:00",
      txn_date: "2026-11-18",
      amount_idr: 200000,
      description: "DONE CONTOH TWO",
      invoice_number: "INV-000000-00002",
      client_name: "PT CONTOH DUA",
    },
    {
      txn_id: 103,
      action: "linked" as const,
      done_at: "2026-11-18T08:03:00+08:00",
      txn_date: "2026-11-18",
      amount_idr: 300000,
      description: "DONE CONTOH THREE",
      invoice_number: "INV-000000-00003",
      client_name: "PT CONTOH TIGA",
    },
    {
      txn_id: 104,
      action: "ignored" as const,
      done_at: "2026-11-18T08:04:00+08:00",
      txn_date: "2026-11-18",
      amount_idr: 400000,
      description: "DONE CONTOH FOUR",
      invoice_number: null,
      client_name: null,
    },
  ],
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
        candidates: [openCandidate, otherYearCandidate],
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

const renderCentre = (
  onOpenCashout = vi.fn(),
  onConfirmed = vi.fn(),
  onOpenOutgoings = vi.fn(),
) => {
  render(
    <ReconciliationInbox
      onConfirmed={onConfirmed}
      doneVersion={0}
      onOpenCashout={onOpenCashout}
      onOpenOutgoings={onOpenOutgoings}
    />,
  );
  return { onOpenCashout, onConfirmed, onOpenOutgoings };
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
    for (const [key, text] of [
      ["confirm", "CONTOH TRANSFER ONE"],
      ["link", "CONTOH TRANSFER TWO"],
      ["choose", "CONTOH TRANSFER THREE"],
      ["nomatch", "CONTOH TRANSFER FOUR"],
    ] as const) {
      fireEvent.click(tab(key));
      expect((await screen.findAllByText(text)).length).toBeGreaterThan(0);
    }
    fireEvent.click(tab("choose"));
    expect(await screen.findByText("(no memo)")).toBeInTheDocument();
  });

  it("selects the first non-empty tab and applies its situation classes", async () => {
    renderCentre();
    await ready();
    const active = tab("confirm");
    const inactive = tab("link");
    const activeBadge = screen.getByTestId("cc-count-confirm");
    const inactiveBadge = screen.getByTestId("cc-count-link");

    expect(active).toHaveAttribute("aria-selected", "true");
    for (const key of ["link", "choose", "nomatch", "done"])
      expect(tab(key)).toHaveAttribute("aria-selected", "false");
    expect(active).toHaveClass(
      "cc-s-confirm",
      "inline-flex",
      "h-[42px]",
      "items-center",
      "gap-2.5",
      "rounded-[12px]",
      "px-4",
      "text-base",
      "font-semibold",
      "bg-[var(--cc-solid)]",
      "text-[var(--cc-on)]",
    );
    expect(inactive).toHaveClass(
      "cc-s-link",
      "inline-flex",
      "h-[42px]",
      "items-center",
      "gap-2.5",
      "rounded-[12px]",
      "px-4",
      "text-base",
      "font-semibold",
      "text-[var(--cc-tx)]",
      "hover:bg-[var(--cc-sel)]",
    );
    expect(activeBadge).toHaveClass(
      "inline-flex",
      "h-6",
      "min-w-6",
      "items-center",
      "justify-center",
      "rounded-[12px]",
      "px-[7px]",
      "font-serif",
      "text-sm",
      "font-semibold",
      "tabular-nums",
      "bg-[var(--cc-on)]",
      "text-[var(--cc-solid)]",
    );
    expect(inactiveBadge).toHaveClass(
      "inline-flex",
      "h-6",
      "min-w-6",
      "items-center",
      "justify-center",
      "rounded-[12px]",
      "px-[7px]",
      "font-serif",
      "text-sm",
      "font-semibold",
      "tabular-nums",
      "bg-[var(--cc-pill)]",
      "text-[var(--cc-tx)]",
    );
    expect(opened()).toHaveClass("cc-s-confirm");
  });

  it("disables a zero tab with a tick and no badge", async () => {
    api.getReconciliationInbox.mockResolvedValue([rows()[0]]);
    renderCentre();
    await ready();
    expect(tab("link")).toBeDisabled();
    expect(tab("link").querySelector('[data-glyph="tick"]')).not.toBeNull();
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

  it("Money out opens the outgoings view and changes nothing here", async () => {
    const { onOpenOutgoings, onOpenCashout } = renderCentre();
    await ready();
    await screen.findByTestId("cc-count-done");
    const moneyOut = screen.getByTestId("cc-money-out");
    expect(moneyOut.tagName).toBe("BUTTON");
    expect(moneyOut).not.toHaveAttribute("role");
    expect(moneyOut).toHaveTextContent(/^Money out$/);
    const requestsBefore =
      api.getReconciliationInbox.mock.calls.length +
      api.getMatchCandidates.mock.calls.length +
      api.getBankTransactions.mock.calls.length +
      api.getDoneToday.mock.calls.length +
      api.linkRecorded.mock.calls.length +
      api.ignoreTransaction.mock.calls.length +
      api.reopenTransaction.mock.calls.length +
      api.confirm.mock.calls.length +
      api.previewBankStatement.mock.calls.length +
      api.importBankStatement.mock.calls.length;

    fireEvent.click(moneyOut);

    expect(onOpenOutgoings).toHaveBeenCalledTimes(1);
    expect(onOpenCashout).not.toHaveBeenCalled();
    expect(tab("confirm")).toHaveAttribute("aria-selected", "true");
    expect(tab("done")).toHaveAttribute("aria-selected", "false");
    expect(screen.queryByTestId("cc-done")).toBeNull();
    expect(
      api.getReconciliationInbox.mock.calls.length +
        api.getMatchCandidates.mock.calls.length +
        api.getBankTransactions.mock.calls.length +
        api.getDoneToday.mock.calls.length +
        api.linkRecorded.mock.calls.length +
        api.ignoreTransaction.mock.calls.length +
        api.reopenTransaction.mock.calls.length +
        api.confirm.mock.calls.length +
        api.previewBankStatement.mock.calls.length +
        api.importBankStatement.mock.calls.length,
    ).toBe(requestsBefore);
  });

  it("shows the Done tab count and the done view", async () => {
    renderCentre();
    await ready();
    expect(screen.getByTestId("cc-count-done")).toHaveTextContent("4");
    for (const item of done.items)
      expect(screen.queryByTestId(`done-today-item-${item.txn_id}`)).toBeNull();
    fireEvent.click(tab("done"));
    const doneView = screen.getByTestId("cc-done");
    expect(doneView).toHaveTextContent("Done today");
    expect(doneView).toHaveClass("cc-done");
    for (const item of done.items)
      expect(
        within(doneView).getByTestId(`done-today-item-${item.txn_id}`),
      ).toBeInTheDocument();
    expect(
      within(doneView).getByText("Linked and ignored"),
    ).toBeInTheDocument();
    fireEvent.click(tab("confirm"));
    expect(screen.queryByTestId("cc-done")).toBeNull();
    for (const item of done.items)
      expect(screen.queryByTestId(`done-today-item-${item.txn_id}`)).toBeNull();
    await within(opened()).findByText("Our invoice");
  });

  it("removes Done today when the route is unavailable", async () => {
    api.getDoneToday.mockRejectedValue(
      Object.assign(new Error("not found"), { statusCode: 404 }),
    );
    renderCentre();
    await ready();
    await waitFor(() => expect(screen.queryByTestId("cc-tab-done")).toBeNull());
  });

  it("leaves the Done view when a later load says the route is unavailable", async () => {
    const props = {
      onConfirmed: vi.fn(),
      onOpenCashout: vi.fn(),
      onOpenOutgoings: vi.fn(),
    };
    const { rerender } = render(
      <ReconciliationInbox {...props} doneVersion={0} />,
    );
    await ready();
    fireEvent.click(await screen.findByRole("tab", { name: /done today/i }));
    await screen.findByTestId("cc-done");

    api.getDoneToday.mockRejectedValueOnce(
      Object.assign(new Error("Not Found"), { statusCode: 404 }),
    );
    rerender(<ReconciliationInbox {...props} doneVersion={1} />);

    await waitFor(() => {
      expect(screen.queryByRole("tab", { name: /done today/i })).toBeNull();
      expect(screen.queryByTestId("cc-done")).toBeNull();
      expect(screen.getByRole("tab", { name: /to confirm/i })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(screen.getByTestId("inbox-row-1")).toHaveAttribute(
        "aria-current",
        "true",
      );
    });
  });

  it("renders date blocks, empty memos and raw dates", async () => {
    renderCentre();
    await ready();
    const validRow = screen.getByTestId("inbox-row-1");
    expect(validRow).toHaveTextContent("16Nov");
    expect(within(validRow).getByText("16")).toBeInTheDocument();
    expect(within(validRow).getByText("Nov")).toBeInTheDocument();
    fireEvent.click(tab("choose"));
    const rawRow = screen.getByTestId("inbox-row-5");
    expect(rawRow).toHaveTextContent("raw date");
    expect(rawRow).toHaveTextContent("(no memo)");
    expect((rawRow.textContent ?? "").match(/raw date/g)).toHaveLength(1);
    expect(within(rawRow).getByText("raw date")).toHaveClass("text-xs");
    expect(
      Array.from(rawRow.querySelectorAll("span")).some((element) =>
        element.classList.contains("mt-[3px]"),
      ),
    ).toBe(false);
    expect(
      Array.from(rawRow.querySelectorAll("b")).some(
        (element) =>
          element.classList.contains("font-serif") &&
          element.classList.contains("text-xl"),
      ),
    ).toBe(false);
  });

  it("names the payer first when the bank line carries one, the memo under it", async () => {
    renderCentre();
    await ready();
    const named = screen.getByTestId("inbox-row-1");
    expect(within(named).getByText("CONTOH PEMBAYAR")).toHaveClass(
      "truncate",
      "text-sm",
      "font-medium",
    );
    expect(within(named).getByText("CONTOH TRANSFER ONE")).toHaveClass(
      "truncate",
      "text-xs",
    );
    fireEvent.click(tab("nomatch"));
    const unnamed = screen.getByTestId("inbox-row-4");
    expect(within(unnamed).getByText("CONTOH TRANSFER FOUR")).toHaveClass(
      "line-clamp-2",
      "font-medium",
    );
  });

  it("marks only the opened row", async () => {
    renderCentre();
    await ready();
    const row = screen.getByTestId("inbox-row-1");
    expect(row).toHaveAttribute("aria-current", "true");
    expect(row).toHaveClass("bg-[var(--cc-sel)]");
    expect(within(row).getByTestId("cc-marker")).toBeInTheDocument();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    const newlyOpened = screen.getByTestId("inbox-row-3");
    expect(newlyOpened).toHaveAttribute("aria-current", "true");
    const visibleRows = within(
      screen.getByRole("list", { name: "Transfers" }),
    ).getAllByRole("button");
    for (const other of visibleRows.filter(
      (candidateRow) => candidateRow !== newlyOpened,
    )) {
      expect(other).not.toHaveAttribute("aria-current");
      expect(other).not.toHaveClass("bg-[var(--cc-sel)]");
      expect(within(other).queryByTestId("cc-marker")).toBeNull();
    }
  });

  it("loads the automatically opened transfer and does not reload on a re-click", async () => {
    api.getReconciliationInbox.mockResolvedValue([
      ...rows(),
      inboxItem({
        txn_id: 6,
        description: "CONTOH TRANSFER SIX",
        light: "green",
      }),
    ]);
    renderCentre();
    await ready();
    await waitFor(() => {
      expect(api.getMatchCandidates).toHaveBeenCalledTimes(1);
      expect(api.getMatchCandidates).toHaveBeenCalledWith(1);
    });
    fireEvent.click(screen.getByTestId("inbox-row-6"));
    await waitFor(() => {
      expect(api.getMatchCandidates).toHaveBeenCalledTimes(2);
      expect(api.getMatchCandidates).toHaveBeenLastCalledWith(6);
    });
    fireEvent.click(screen.getByTestId("inbox-row-6"));
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(2);
  });

  it("shows the band and pair view with every reason", async () => {
    renderCentre();
    await ready();
    expect(
      await screen.findByText("One invoice matches this transfer"),
    ).toBeInTheDocument();
    const pair = opened();
    await within(pair).findByText("Our invoice");
    expect(pair).toHaveTextContent("From the bank");
    expect(pair).toHaveTextContent("Rp12.500.000");
    expect(pair).toHaveTextContent("Received Mon 16 Nov 2026");
    expect(within(pair).getByText("=")).toBeInTheDocument();
    expect(pair).toHaveTextContent("Rp12.400.000");
    expect(pair).toHaveTextContent("Example service · issued 6 Nov");
    expect(pair).toHaveTextContent(
      "same amount · number in memo · same client",
    );
    expect(pair.querySelector('[data-glyph="tick"]')).not.toBeNull();

    fireEvent.click(tab("link"));
    const alreadyPaidPair = opened();
    await within(alreadyPaidPair).findByText("Our invoice");
    expect(alreadyPaidPair).toHaveTextContent("Already paid");
    expect(alreadyPaidPair.querySelector('[data-glyph="tick"]')).not.toBeNull();
  });

  it.each([
    ["confirm", "One invoice matches this transfer", "confirm", "cc-s-confirm"],
    ["link", "This payment looks already recorded", "link", "cc-s-link"],
    ["choose", "Which invoice is this payment for?", "choose", "cc-s-choose"],
    ["nomatch", "No invoice has this amount", "nomatch", "cc-s-nomatch"],
  ])(
    "pins the %s band title, glyph and situation class",
    async (key, title, glyph, situationClass) => {
      renderCentre();
      await ready();
      fireEvent.click(tab(key));
      const band = await screen.findByTestId("cc-band");
      expect(band).toHaveTextContent(title);
      expect(band).toHaveClass(
        "flex",
        "h-16",
        "flex-none",
        "items-center",
        "gap-3.5",
        "px-7",
        "bg-[var(--cc-solid)]",
        "text-[var(--cc-on)]",
      );
      expect(band.querySelector(`[data-glyph="${glyph}"]`)).not.toBeNull();
      expect(opened()).toHaveClass(situationClass);
      expect(screen.getByRole("list", { name: "Transfers" })).toHaveClass(
        situationClass,
      );
    },
  );

  it("opens the table without a request when Choose another is clicked", async () => {
    renderCentre();
    await ready();
    await screen.findByText("One invoice matches this transfer");
    const calls = api.getMatchCandidates.mock.calls.length;
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    expect(screen.getByTestId("cc-cand-11")).toBeInTheDocument();
    expect(api.getMatchCandidates).toHaveBeenCalledTimes(calls);
  });

  it("shows every candidate field, all reasons and the missing-number fallback", async () => {
    renderCentre();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    const row = screen.getByTestId("cc-cand-11");
    expect(row).toHaveTextContent("INV-000000-00001");
    expect(within(row).getByText("6 Nov")).toBeInTheDocument();
    expect(within(row).queryByText("6 Nov 2026")).toBeNull();
    expect(row).toHaveTextContent("Example service");
    expect(row).toHaveTextContent("PT CONTOH SATU");
    expect(row).toHaveTextContent("Rp12.400.000");
    expect(row).toHaveTextContent("same amount · number in memo · same client");
    const otherYearRow = screen.getByTestId("cc-cand-13");
    expect(within(otherYearRow).getByText("6 Nov 2025")).toBeInTheDocument();
    fireEvent.click(tab("link"));
    await screen.findByText("This payment looks already recorded");
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    expect(screen.getByTestId("cc-cand-12")).toHaveTextContent(
      "No invoice number",
    );
  });

  it("preselects nothing on To choose until a radio is chosen", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    const primary = await screen.findByRole("button", {
      name: "Confirm payment",
    });
    expect(primary).toBeDisabled();
    fireEvent.click(
      within(screen.getByTestId("cc-cand-11")).getByRole("radio"),
    );
    expect(
      screen.getByRole("button", { name: "Confirm payment" }),
    ).toBeEnabled();
  });

  it("switches the primary label and footer when a recorded candidate is picked", async () => {
    api.getMatchCandidates.mockImplementation(async (id) =>
      id === 3
        ? {
            ...emptyCandidates(id),
            candidates: [openCandidate],
            recorded_candidates: [recordedCandidate],
          }
        : emptyCandidates(id),
    );
    renderCentre();
    await ready();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    fireEvent.click(
      within(screen.getByTestId("cc-cand-12")).getByRole("radio"),
    );
    expect(
      screen.getByRole("button", { name: "Link payment" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("cc-foot")).getByText(
        "Linking adds this transfer to the cash-out and dates the invoice; the case does not change. You can reopen this transfer any time from Done today.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByTestId("cc-cand-11")).getByRole("radio"),
    );
    expect(
      screen.getByRole("button", { name: "Confirm payment" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("cc-foot")).getByText(
        "Confirming records this payment on the practice, the invoice and the cashout. You can undo it today from Done today. Paid in full, a case waiting for payment moves to On Process.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the nothing view for a No match transfer", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("nomatch"));
    expect(await screen.findByText("Nothing found")).toBeInTheDocument();
    expect(opened()).toHaveTextContent("≠");
    expect(opened()).toHaveTextContent("Our side");
    expect(opened()).toHaveTextContent("No open or paid invoice of Rp500.000.");
    expect(opened()).toHaveTextContent(
      "If it is not a client payment, ignore it with a reason.",
    );
    expect(
      within(screen.getByTestId("cc-foot")).getByText(
        "Ignoring moves no money. You can reopen this transfer any time from Done today.",
      ),
    ).toBeInTheDocument();
    const ignoreWithReason = screen.getByRole("button", {
      name: "Ignore with a reason",
    });
    expect(ignoreWithReason).toHaveClass(
      "inline-flex",
      "h-11",
      "shrink-0",
      "items-center",
      "justify-center",
      "gap-2",
      "whitespace-nowrap",
      "rounded-[10px]",
      "px-[22px]",
      "text-sm",
      "font-bold",
      "bg-[var(--cc-act)]",
      "text-[var(--cc-act-on)]",
      "disabled:opacity-50",
    );
    fireEvent.click(ignoreWithReason);
    expect(screen.getByTestId("cc-ignore-form")).toBeInTheDocument();
  });

  it("wraps Skip for now and disables it for a single row", async () => {
    renderCentre();
    await ready();
    fireEvent.click(tab("choose"));
    await screen.findByText("Which invoice is this payment for?");
    expect(screen.getByRole("button", { name: "Skip for now" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() =>
      expect(screen.getByTestId("inbox-row-5")).toHaveAttribute(
        "aria-current",
        "true",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() =>
      expect(screen.getByTestId("inbox-row-3")).toHaveAttribute(
        "aria-current",
        "true",
      ),
    );
    fireEvent.click(tab("nomatch"));
    expect(
      await screen.findByRole("button", { name: "Skip for now" }),
    ).toBeDisabled();
  });

  it("disables TABLE VIEW Skip for now with one row", async () => {
    api.getReconciliationInbox.mockResolvedValue([rows()[2]]);
    renderCentre();
    await screen.findByText("CONTOH TRANSFER THREE");
    expect(
      await screen.findByRole("button", { name: "Skip for now" }),
    ).toBeDisabled();
  });

  it("forgets Choose another when another transfer opens", async () => {
    const second = inboxItem({
      ...rows()[0],
      txn_id: 6,
      description: "CONTOH TRANSFER SIX",
    });
    api.getReconciliationInbox.mockResolvedValue([rows()[0], second]);
    api.getMatchCandidates.mockImplementation(async (id) => ({
      ...emptyCandidates(id),
      candidates: [openCandidate],
    }));
    renderCentre();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    expect(screen.getByTestId("cc-cand-11")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("inbox-row-6"));
    await screen.findAllByText("CONTOH TRANSFER SIX");
    fireEvent.click(screen.getByTestId("inbox-row-1"));
    await waitFor(() =>
      expect(api.getMatchCandidates).toHaveBeenLastCalledWith(1),
    );
    const pairAgain = opened();
    await within(pairAgain).findByText("Our invoice");
    expect(pairAgain).toHaveTextContent("From the bank");
    expect(pairAgain).toHaveTextContent("Received Mon 16 Nov 2026");
    expect(pairAgain).toHaveTextContent("Rp12.400.000");
    expect(
      within(pairAgain).getByRole("button", { name: "Choose another" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("cc-cand-11")).toBeNull();
    expect(screen.queryByTestId("cc-cand-13")).toBeNull();
  });

  it("renders Rp separately with the pinned amount classes", async () => {
    renderCentre();
    await ready();
    const amount = within(screen.getByTestId("inbox-row-1")).getByText("Rp");
    expect(amount).toHaveClass(
      "mr-[0.28em]",
      "text-[0.72em]",
      "font-medium",
      "text-[var(--cc-tx-2)]",
    );
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

  it("shows the empty bank-date state", async () => {
    api.getBankTransactions.mockResolvedValueOnce([]);
    const first = renderCentre();
    await ready();
    expect(screen.getByText("No bank data yet")).toBeInTheDocument();
    first.onOpenCashout.mockClear();
  });

  it("shows the failed bank-date state", async () => {
    api.getBankTransactions.mockRejectedValueOnce(new Error("date failed"));
    renderCentre();
    await ready();
    expect(screen.getByTestId("cc-bank-date")).toHaveTextContent(
      "Bank data date could not be loaded.",
    );
  });

  it("82: a non-array bank response is failed and keeps the empty inbox calm", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getBankTransactions.mockResolvedValueOnce({} as never);
    renderCentre();

    expect(await screen.findByTestId("cc-calm")).toBeInTheDocument();
    expect(screen.getByTestId("cc-bank-date")).toHaveTextContent(
      "Bank data date could not be loaded.",
    );
    expect(screen.queryByTestId("cc-dayone")).toBeNull();
  });

  it("83: negative amounts put the minus before Rp and positive amounts do not", () => {
    render(
      <div>
        <span data-testid="negative-amount">
          <CcAmount value={-1234} />
        </span>
        <span data-testid="positive-amount">
          <CcAmount value={1234} />
        </span>
      </div>,
    );

    expect(screen.getByTestId("negative-amount")).toHaveTextContent("-Rp1.234");
    expect(screen.getByTestId("positive-amount")).toHaveTextContent("Rp1.234");
    expect(screen.getByTestId("positive-amount")).not.toHaveTextContent("-");
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

  it("disables Upload statement while previewing and importing", async () => {
    const preview = deferred<ReturnType<typeof previewFixture>>();
    const imported = deferred<ReturnType<typeof importFixture>>();
    api.previewBankStatement.mockReturnValueOnce(preview.promise);
    api.importBankStatement.mockReturnValueOnce(imported.promise);
    renderCentre();
    await ready();
    const upload = screen.getByRole("button", { name: "Upload statement" });
    expect(upload).toBeEnabled();

    fireEvent.change(screen.getByTestId("bank-statement-input"), {
      target: { files: [new File(["x"], "statement.csv")] },
    });
    await waitFor(() => {
      expect(api.previewBankStatement).toHaveBeenCalledTimes(1);
      expect(upload).toBeDisabled();
    });

    await act(async () => preview.resolve(previewFixture()));
    await waitFor(() => expect(upload).toBeEnabled());

    fireEvent.click(
      await screen.findByRole("button", { name: /Import 3 transactions/i }),
    );
    await waitFor(() => {
      expect(api.importBankStatement).toHaveBeenCalledTimes(1);
      expect(upload).toBeDisabled();
    });

    await act(async () => imported.resolve(importFixture()));
    await waitFor(() => expect(upload).toBeEnabled());
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
    expect(screen.getByTestId("cc-calm")).toHaveTextContent(
      "Bank data is in through Mon 16 Nov.",
    );
    expect(
      screen.getByRole("button", { name: "See what was done today" }),
    ).toBeInTheDocument();
  });

  it("uses the singular handled count in the calm view", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getDoneToday.mockResolvedValue({
      ...done,
      counts: { confirmed: 1, linked: 0, ignored: 0, total: 1 },
      items: [done.items[0]],
    });
    renderCentre();
    const calm = await screen.findByTestId("cc-calm");
    expect(within(calm).getByText(/1 transfer handled\./)).toBeInTheDocument();
  });

  it("omits the handled count when Done today has no data", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getDoneToday.mockResolvedValue(null as never);
    renderCentre();
    const calm = await screen.findByTestId("cc-calm");
    expect(within(calm).queryByText(/transfers? handled\./)).toBeNull();
  });

  it("hides the calm-view Done action when the route is unavailable", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getDoneToday.mockRejectedValue(
      Object.assign(new Error("not found"), { statusCode: 404 }),
    );
    renderCentre();
    expect(await screen.findByTestId("cc-calm")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getDoneToday).toHaveBeenCalledTimes(1);
      expect(
        screen.queryByRole("button", { name: "See what was done today" }),
      ).toBeNull();
    });
  });

  it("opens Done today from the calm view when the route is available", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    renderCentre();
    expect(await screen.findByTestId("cc-calm")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "See what was done today" }),
    );
    expect(await screen.findByTestId("cc-done")).toBeInTheDocument();
  });

  it("advances when a tab empties and shows calm when all tabs empty", async () => {
    const initial = [rows()[0], rows()[1]];
    api.getReconciliationInbox
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce([rows()[1]])
      .mockResolvedValueOnce([]);
    api.ignoreTransaction.mockResolvedValue({} as never);
    renderCentre();
    await ready();
    fireEvent.click(tab("confirm"));
    expect(tab("confirm")).toHaveAttribute("aria-selected", "true");

    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "example reason one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(1));
    await waitFor(() => {
      expect(tab("link")).toHaveAttribute("aria-selected", "true");
      expect(screen.getByTestId("inbox-row-2")).toHaveAttribute(
        "aria-current",
        "true",
      );
    });
    expect(opened()).toHaveTextContent("CONTOH TRANSFER TWO");

    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    fireEvent.change(screen.getByLabelText("Reason for ignoring"), {
      target: { value: "example reason two" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ignore transfer" }));
    await waitFor(() => expect(api.ignoreTransaction).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("cc-calm")).toBeInTheDocument();
    expect(screen.queryByTestId("cc-opened")).toBeNull();
  });

  it("uses calm, not day one, when the bank-date request fails", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getBankTransactions.mockRejectedValue(new Error("date failed"));
    renderCentre();
    const calm = await screen.findByTestId("cc-calm");
    expect(calm).not.toHaveTextContent("Bank data is in through");
    expect(screen.queryByTestId("cc-dayone")).toBeNull();
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
    expect(card).toHaveTextContent("Cash-out register");
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

  it("disables every situation tab without badge or glyph before the first answer", () => {
    api.getReconciliationInbox.mockReturnValue(new Promise(() => {}));
    renderCentre();
    for (const key of ["confirm", "link", "choose", "nomatch"]) {
      expect(tab(key)).toBeDisabled();
      expect(tab(key).querySelector("[data-glyph]")).toBeNull();
      expect(screen.queryByTestId(`cc-count-${key}`)).toBeNull();
    }
  });

  it("pins nowrap buttons, footer flex sizing and fixed-height scroll classes", async () => {
    renderCentre();
    await ready();
    await screen.findByTestId("cc-foot");
    const expectPinnedButtons = (testId: string) => {
      const container = screen.getByTestId(testId);
      for (const button of within(container).getAllByRole("button"))
        expect(button).toHaveClass("shrink-0", "whitespace-nowrap");
    };
    expectPinnedButtons("cc-header");
    expectPinnedButtons("cc-opened");
    expectPinnedButtons("cc-foot");
    const foot = screen.getByTestId("cc-foot");
    expect(foot.firstElementChild).toHaveClass("flex-1", "min-w-0");
    expect(foot.lastElementChild).toHaveClass("flex-none");

    fireEvent.click(screen.getByRole("button", { name: "Choose another" }));
    expectPinnedButtons("cc-opened");

    fireEvent.click(tab("nomatch"));
    await screen.findByText("Nothing found");
    expectPinnedButtons("cc-opened");

    api.getMatchCandidates.mockRejectedValueOnce(
      new Error("candidate failure"),
    );
    fireEvent.click(tab("link"));
    await screen.findByText("Could not load the invoices for this transfer.");
    expectPinnedButtons("cc-opened");

    fireEvent.click(tab("confirm"));
    fireEvent.click(await screen.findByRole("button", { name: "Ignore" }));
    await screen.findByTestId("cc-ignore-form");
    expectPinnedButtons("cc-opened");
    expect(screen.getByTestId("cc-work")).toHaveClass(
      "h-[550px]",
      "grid-rows-[minmax(0,1fr)]",
    );
    expect(screen.getByRole("list", { name: "Transfers" })).toHaveClass(
      "min-h-0",
      "overflow-y-auto",
    );
    expect(screen.getByTestId("cc-scroll")).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
    );
  });

  it("pins calm and day-one art and button classes", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    renderCentre();
    const calm = await screen.findByTestId("cc-calm");
    expect(calm.querySelector('[data-art="calm"]')).not.toBeNull();
    for (const button of within(calm).getAllByRole("button"))
      expect(button).toHaveClass("shrink-0", "whitespace-nowrap");
  });

  it("pins day-one art and CTA button classes", async () => {
    api.getReconciliationInbox.mockResolvedValue([]);
    api.getBankTransactions.mockResolvedValue([]);
    renderCentre();
    const dayone = await screen.findByTestId("cc-dayone");
    expect(dayone.querySelector('[data-art="dayone"]')).not.toBeNull();
    for (const button of within(dayone).getAllByRole("button"))
      expect(button).toHaveClass("shrink-0", "whitespace-nowrap");
  });

  it("uses mock glyphs in tabs, band, footer, header and cash-out card", async () => {
    renderCentre();
    await ready();
    await screen.findByTestId("cc-foot");
    expect(
      screen.getByTestId("cc-band").querySelector('[data-glyph="confirm"]'),
    ).not.toBeNull();
    expect(
      screen.getByTestId("cc-foot").querySelector('[data-glyph="lock"]'),
    ).not.toBeNull();
    expect(
      screen.getByTestId("cc-header").querySelector('[data-glyph="upload"]'),
    ).not.toBeNull();
    expect(
      screen.getByTestId("cc-cashout-card").querySelector('[data-glyph="out"]'),
    ).not.toBeNull();
    fireEvent.click(tab("link"));
    await screen.findByText("This payment looks already recorded");
    expect(
      screen.getByTestId("cc-foot").querySelector('[data-glyph="undo"]'),
    ).not.toBeNull();
    fireEvent.click(tab("nomatch"));
    await screen.findByText("Nothing found");
    expect(
      screen.getByTestId("cc-foot").querySelector('[data-glyph="undo"]'),
    ).not.toBeNull();
  });

  it("renders cc-more with only the cash-out card", async () => {
    renderCentre();
    await ready();
    const more = screen.getByTestId("cc-more");
    expect(more.children).toHaveLength(1);
    expect(more.firstElementChild).toBe(screen.getByTestId("cc-cashout-card"));
  });

  it("pins compact candidate row and reasons classes", async () => {
    renderCentre();
    await ready();
    fireEvent.click(
      await screen.findByRole("button", { name: "Choose another" }),
    );
    expect(screen.getByTestId("cc-cand-11")).toHaveClass("min-h-10", "py-1");
    expect(screen.getByTestId("cc-reasons-11")).toHaveClass("leading-4");
  });
});
