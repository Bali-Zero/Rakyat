/**
 * Outgoings (owner 2026-11-21/22): list, state switch, and the Justify / Undo actions.
 * Names, codes and amounts are invented.
 */

import {
  render,
  screen,
  fireEvent,
  within,
  waitFor,
} from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { OutgoingsView } from "../_components/OutgoingsView";
import {
  accountingApi,
  type OutgoingsResponse,
} from "@/lib/api/workspace/accounting.api";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
// 2 instead of the server's 500, so one test can show the batch keeps the newest only.
vi.mock("../_components/pnbpBatch", () => ({ PNBP_BATCH_LIMIT: 2 }));
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    getOutgoings: vi.fn(),
    justifyOutgoing: vi.fn(),
    undoOutgoingJustification: vi.fn(),
    justifyPnbpBatch: vi.fn(),
    undoOutgoingBatch: vi.fn(),
  },
}));

const api = vi.mocked(accountingApi);

const DATA: OutgoingsResponse = {
  items: [
    {
      id: 3,
      txn_date: "2026-11-21",
      posted_at: "2026-11-21T13:19:40",
      amount_idr: 2500,
      kind: "transfer_fee",
      description: "OCTO Biz TO CONTOH PENERIMA",
      company_key: "bayu",
      reading: {
        label: "Transfer fee",
        payee: "CONTOH PENERIMA",
        detail: "fee of the transfer to CONTOH PENERIMA",
      },
      fee_of_txn_id: 2,
      state: "to_justify",
      justification: null,
    },
    {
      id: 2,
      txn_date: "2026-11-21",
      posted_at: "2026-11-21T13:19:40",
      amount_idr: 12000000,
      kind: "transfer_out",
      description: "OCTO Biz TO CONTOH PENERIMA",
      company_key: "bayu",
      reading: {
        label: "Transfer",
        payee: "CONTOH PENERIMA",
        purpose: "Reimburse PNBP",
        detail: "Reimburse PNBP",
      },
      fee_of_txn_id: null,
      state: "to_justify",
      justification: null,
    },
    {
      id: 1,
      txn_date: "2026-11-20",
      posted_at: "2026-11-20T21:04:35",
      amount_idr: 5000000,
      kind: "government_fee",
      description: "EMPN BILLING  820261120000001",
      company_key: "bayu",
      reading: {
        label: "PNBP",
        billing_code: "820261120000001",
        detail: "e-billing 820261120000001",
      },
      fee_of_txn_id: null,
      state: "to_justify",
      justification: null,
    },
  ],
  by_kind: [
    { kind: "transfer_out", label: "Transfer", n: 1, total_idr: 12000000 },
    { kind: "government_fee", label: "PNBP", n: 1, total_idr: 5000000 },
    { kind: "transfer_fee", label: "Transfer fee", n: 1, total_idr: 2500 },
  ],
  n: 3,
  total_idr: 17002500,
  n_justified: 2,
  justified_total_idr: 1300000,
};

const todayIso = () => new Date().toISOString();
const JUSTIFIED: OutgoingsResponse = {
  items: [
    {
      id: 6,
      txn_date: "2026-11-19",
      posted_at: "2026-11-19T10:00:00",
      amount_idr: 1000000,
      kind: "transfer_out",
      description: "OCTO Biz TO CONTOH LAIN",
      company_key: "bayu",
      reading: { label: "Transfer", payee: "CONTOH LAIN", detail: "notary" },
      fee_of_txn_id: null,
      state: "justified",
      justification: {
        category: "vendor_case",
        practice_id: 321,
        recipient: null,
        note: "Deed for a test company",
        lines: [],
        by: "staff@example.test",
        at: todayIso(),
      },
    },
    {
      id: 7,
      txn_date: "2026-11-19",
      posted_at: "2026-11-19T10:00:00",
      amount_idr: 2500,
      kind: "transfer_fee",
      description: "OCTO Biz TO CONTOH LAIN",
      company_key: "bayu",
      reading: { label: "Transfer fee", payee: "CONTOH LAIN", detail: "fee" },
      fee_of_txn_id: 6,
      state: "justified",
      justification: {
        category: "vendor_case",
        practice_id: 321,
        recipient: null,
        note: null,
        lines: [],
        by: "staff@example.test",
        at: todayIso(),
        follows_txn_id: 6,
      },
    },
    {
      id: 5,
      txn_date: "2026-11-01",
      posted_at: "2026-11-01T10:00:00",
      amount_idr: 300000,
      kind: "bank_fee",
      description: "ADMIN FEE",
      company_key: "bayu",
      reading: { label: "Bank charge", detail: "admin fee" },
      fee_of_txn_id: null,
      state: "justified",
      justification: {
        category: "bank_charges",
        practice_id: null,
        recipient: null,
        note: null,
        lines: [],
        by: "staff@example.test",
        at: "2026-11-02T03:00:00+00:00",
      },
    },
  ],
  by_kind: [],
  n: 3,
  total_idr: 17002500,
  n_justified: 3,
  justified_total_idr: 1302500,
};
const CASH: OutgoingsResponse = {
  items: [
    {
      id: 9,
      txn_date: "2026-11-21",
      posted_at: "2026-11-21T09:00:00",
      amount_idr: 1000000,
      kind: "cash_withdrawal",
      description: "BDS CLEARING",
      company_key: "bayu",
      reading: { label: "Cash withdrawal", detail: "cheque 0001" },
      fee_of_txn_id: null,
      state: "to_justify",
      justification: null,
    },
  ],
  by_kind: [
    {
      kind: "cash_withdrawal",
      label: "Cash withdrawal",
      n: 1,
      total_idr: 1000000,
    },
  ],
  n: 1,
  total_idr: 1000000,
  n_justified: 0,
  justified_total_idr: 0,
};

const dialog = () =>
  screen.getByRole("dialog", { name: "Justify this outgoing" });
const field = (name: string) => within(dialog()).getByLabelText(name);
const save = () => within(dialog()).getByRole("button", { name: "Save" });
const type = (name: string, value: string) =>
  fireEvent.change(field(name), { target: { value } });
const addLine = () =>
  fireEvent.click(within(dialog()).getByRole("button", { name: /Add a line/ }));

const row = (id: number) => screen.getByTestId(`outgoing-${id}`);
const cells = (id: number) =>
  within(row(id))
    .getAllByRole("cell")
    .map((c) => c.textContent?.replace(/\s+/g, " ").trim());

beforeEach(() => {
  vi.resetAllMocks();
});

describe("OutgoingsView", () => {
  it("lists every outgoing newest first with what the bank wrote", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    expect(
      screen.getAllByTestId(/^outgoing-\d+$/).map((r) => r.dataset.testid),
    ).toEqual(["outgoing-3", "outgoing-2", "outgoing-1"]);
    expect(cells(2).slice(0, 4)).toEqual([
      "21 Nov 13:19",
      "Transfer",
      "CONTOH PENERIMA",
      "Reimburse PNBP",
    ]);
    expect(cells(3).slice(1, 4)).toEqual([
      "Transfer fee",
      "CONTOH PENERIMA",
      "fee of the transfer to CONTOH PENERIMA",
    ]);
    expect(cells(1).slice(0, 4)).toEqual([
      "20 Nov 21:04",
      "PNBP",
      "—",
      "e-billing 820261120000001",
    ]);
    expect(cells(2)[4]).toMatch(/12\.000\.000/);
    expect(api.getOutgoings).toHaveBeenCalledWith({ state: "to_justify" });
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  it("filters by kind with the totals of every kind and back to all", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    const group = screen.getByRole("group", { name: "Filter by kind" });
    const buttons = within(group).getAllByRole("button");
    expect(buttons.map((b) => b.textContent?.split(" · ").slice(0, 2))).toEqual(
      [
        ["All", "3"],
        ["Transfer", "1"],
        ["PNBP", "1"],
        ["Transfer fee", "1"],
      ],
    );
    expect(buttons[0]).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(buttons[2]);
    expect(buttons[2]).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByTestId(/^outgoing-\d+$/)).toEqual([row(1)]);
    fireEvent.click(buttons[0]);
    expect(screen.getAllByTestId(/^outgoing-\d+$/)).toHaveLength(3);
  });

  it("says when the list shows only the newest of more", async () => {
    api.getOutgoings.mockResolvedValue({ ...DATA, n: 400 });
    render(<OutgoingsView />);
    expect(
      await screen.findByText("Showing the newest 3 of 400."),
    ).toBeInTheDocument();
  });

  it("says when there is nothing to justify", async () => {
    api.getOutgoings.mockResolvedValue({
      items: [],
      by_kind: [],
      n: 0,
      total_idr: 0,
      n_justified: 0,
      justified_total_idr: 0,
    });
    render(<OutgoingsView />);
    expect(await screen.findByText("Nothing to justify.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("offers to try again after a failed load", async () => {
    api.getOutgoings.mockRejectedValueOnce(new Error("down"));
    api.getOutgoings.mockResolvedValueOnce(DATA);
    render(<OutgoingsView />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the outgoings.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("outgoing-1")).toBeInTheDocument();
    expect(api.getOutgoings).toHaveBeenCalledTimes(2);
  });

  it("switches between to justify and justified with both counts", async () => {
    api.getOutgoings.mockImplementation(async (opts) =>
      opts?.state === "justified" ? JUSTIFIED : DATA,
    );
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    const group = screen.getByRole("group", { name: "Outgoings state" });
    expect(
      within(group).getByRole("button", { name: "To justify (3)" }),
    ).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(group).getByRole("button", { name: /^Justified/ }));
    await screen.findByTestId("outgoing-6");
    expect(api.getOutgoings).toHaveBeenLastCalledWith({ state: "justified" });
    expect(screen.queryByTestId("outgoing-1")).toBeNull();
    expect(
      within(group).getByRole("button", { name: "Justified (3)" }),
    ).toHaveAttribute("aria-pressed", "true");
    const block = screen.getByTestId("justification-6");
    expect(block).toHaveTextContent("Notary / agent / freelancer for a case");
    expect(block).toHaveTextContent("case 321");
    expect(block).toHaveTextContent("Deed for a test company");
    expect(block).toHaveTextContent("staff@example.test");
    // 03:00 UTC on 2 Nov is 11:00 in Bali: the time is shown in WITA, not as stored
    expect(screen.getByTestId("justification-5")).toHaveTextContent("11:00");
    expect(screen.getByTestId("justification-5")).not.toHaveTextContent(
      "03:00",
    );
  });

  it("offers Justify on a row and none on a fee that follows its transfer", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    expect(screen.getByTestId("justify-1")).toBeInTheDocument();
    expect(screen.getByTestId("justify-2")).toBeInTheDocument();
    expect(screen.queryByTestId("justify-3")).toBeNull();
    expect(
      within(row(3)).getByText("Follows its transfer"),
    ).toBeInTheDocument();
  });

  it("preselects the category from the reading and sends exactly the body", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    api.justifyOutgoing.mockResolvedValue({
      txn_id: 1,
      justification_id: 1,
      state: "justified",
    });
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    fireEvent.click(screen.getByTestId("justify-1"));
    expect(field("Category")).toHaveValue("pnbp_case");
    expect(save()).toBeEnabled();
    type("Case id", "4a5b");
    expect(save()).toBeDisabled(); // never read as 45
    type("Case id", "45");
    expect(save()).toBeEnabled();
    fireEvent.click(save());
    await waitFor(() =>
      expect(api.justifyOutgoing).toHaveBeenCalledWith(1, {
        category: "pnbp_case",
        practice_id: 45,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(toast.success).toHaveBeenCalled();
    await waitFor(() => expect(api.getOutgoings).toHaveBeenCalledTimes(2));
  });

  it("needs a note for Other before Save is enabled", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    api.justifyOutgoing.mockResolvedValue({
      txn_id: 2,
      justification_id: 1,
      state: "justified",
    });
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-2");
    fireEvent.click(screen.getByTestId("justify-2"));
    expect(field("Category")).toHaveValue("other");
    expect(save()).toBeDisabled();
    type("Note", "  Office water  ");
    expect(save()).toBeEnabled();
    fireEvent.click(save());
    await waitFor(() =>
      expect(api.justifyOutgoing).toHaveBeenCalledWith(2, {
        category: "other",
        note: "Office water",
      }),
    );
  });

  it("holds a cash withdrawal until recipient, note and lines add up exactly", async () => {
    api.getOutgoings.mockResolvedValue(CASH);
    api.justifyOutgoing.mockResolvedValue({
      txn_id: 9,
      justification_id: 1,
      state: "justified",
    });
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-9");
    fireEvent.click(screen.getByTestId("justify-9"));
    expect(field("Category")).toHaveValue("cash_withdrawal");
    expect(save()).toBeDisabled();
    type("Recipient", "Contoh Penerima");
    type("Note", "Weekly cash");
    expect(save()).toBeDisabled(); // no line yet
    addLine();
    type("Line 1 amount", "600000");
    type("Line 1 note", "Courier");
    expect(screen.getByTestId("justify-remaining").textContent).toMatch(
      /400\.000/,
    );
    expect(save()).toBeDisabled(); // remaining is not 0
    addLine();
    type("Line 2 amount", "500000");
    type("Line 2 note", "Stamps");
    expect(save()).toBeDisabled(); // 100000 too much
    type("Line 2 amount", "400000");
    type("Line 2 case id", "77");
    expect(save()).toBeEnabled();
    type("Recipient", "  ");
    expect(save()).toBeDisabled();
    type("Recipient", "Contoh Penerima");
    fireEvent.click(save());
    await waitFor(() =>
      expect(api.justifyOutgoing).toHaveBeenCalledWith(9, {
        category: "cash_withdrawal",
        recipient: "Contoh Penerima",
        note: "Weekly cash",
        lines: [
          { amount_idr: 600000, category: "other", note: "Courier" },
          {
            amount_idr: 400000,
            category: "other",
            note: "Stamps",
            practice_id: 77,
          },
        ],
      }),
    );
  });

  it("never turns a signed, decimal or foreign amount into another number", async () => {
    api.getOutgoings.mockResolvedValue(CASH);
    api.justifyOutgoing.mockResolvedValue({
      txn_id: 9,
      justification_id: 1,
      state: "justified",
    });
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-9");
    fireEvent.click(screen.getByTestId("justify-9"));
    type("Recipient", "Contoh Penerima");
    type("Note", "Weekly cash");
    addLine();
    type("Line 1 note", "Courier");
    for (const bad of [
      "-1000000",
      "1000000.5",
      "1000000,5",
      "1e6",
      "Rp 1000000",
    ]) {
      type("Line 1 amount", bad);
      expect(save()).toBeDisabled();
    }
    type("Line 1 amount", "1.000.000");
    type("Line 1 case id", "-77");
    expect(save()).toBeDisabled();
    type("Line 1 case id", "77");
    expect(save()).toBeEnabled();
    fireEvent.click(save());
    await waitFor(() =>
      expect(api.justifyOutgoing).toHaveBeenCalledWith(9, {
        category: "cash_withdrawal",
        recipient: "Contoh Penerima",
        note: "Weekly cash",
        lines: [
          {
            amount_idr: 1000000,
            category: "other",
            note: "Courier",
            practice_id: 77,
          },
        ],
      }),
    );
  });

  it("removing a line moves the remaining amount", async () => {
    api.getOutgoings.mockResolvedValue(CASH);
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-9");
    fireEvent.click(screen.getByTestId("justify-9"));
    addLine();
    type("Line 1 amount", "1000000");
    expect(screen.getByTestId("justify-remaining").textContent).not.toMatch(
      /[1-9]/,
    );
    fireEvent.click(
      within(dialog()).getByRole("button", { name: "Remove line 1" }),
    );
    expect(screen.getByTestId("justify-remaining").textContent).toMatch(
      /1\.000\.000/,
    );
  });

  it("shows the server's message as is when justify fails, and keeps the dialog", async () => {
    api.getOutgoings.mockResolvedValue(DATA);
    api.justifyOutgoing.mockRejectedValue(
      new Error("this debit is already justified: undo it first"),
    );
    render(<OutgoingsView />);
    await screen.findByTestId("outgoing-1");
    fireEvent.click(screen.getByTestId("justify-1"));
    fireEvent.click(save());
    expect(await within(dialog()).findByRole("alert")).toHaveTextContent(
      "this debit is already justified: undo it first",
    );
    expect(save()).toBeEnabled();
    expect(api.getOutgoings).toHaveBeenCalledTimes(1);
  });

  it("offers Undo only for a justification made today and not on a followed fee", async () => {
    api.getOutgoings.mockResolvedValue(JUSTIFIED);
    api.undoOutgoingJustification.mockResolvedValue({
      txn_id: 6,
      justification_id: 1,
      state: "to_justify",
    });
    render(<OutgoingsView />);
    fireEvent.click(await screen.findByRole("button", { name: /^Justified/ }));
    await screen.findByTestId("outgoing-6");
    expect(screen.getByTestId("undo-6")).toBeInTheDocument();
    expect(screen.queryByTestId("undo-7")).toBeNull(); // fee follows its transfer
    expect(screen.queryByTestId("undo-5")).toBeNull(); // justified on an earlier day
    fireEvent.click(screen.getByTestId("undo-6"));
    await waitFor(() =>
      expect(api.undoOutgoingJustification).toHaveBeenCalledWith(6),
    );
    expect(toast.success).toHaveBeenCalled();
  });

  it("shows the server's message when undo is refused", async () => {
    api.getOutgoings.mockResolvedValue(JUSTIFIED);
    api.undoOutgoingJustification.mockRejectedValue(
      new Error("justified before today: it cannot be undone"),
    );
    render(<OutgoingsView />);
    fireEvent.click(await screen.findByRole("button", { name: /^Justified/ }));
    await screen.findByTestId("undo-6");
    fireEvent.click(screen.getByTestId("undo-6"));
    expect(await within(row(6)).findByRole("alert")).toHaveTextContent(
      "justified before today: it cannot be undone",
    );
    expect(toast.error).toHaveBeenCalled();
  });

  describe("Justify all PNBP", () => {
    const pnbp = (id: number, day: number, code: string, n: number) => ({
      id,
      txn_date: `2026-11-${day}`,
      posted_at: `2026-11-${day}T10:00:00`,
      amount_idr: 1000000 * id,
      kind: "government_fee",
      description: `EMPN BILLING  ${code}`,
      company_key: "bayu",
      reading: {
        label: "PNBP",
        billing_code: code,
        detail: `e-billing ${code}`,
        code_debits: n,
      },
      fee_of_txn_id: null,
      state: "to_justify" as const,
      justification: null,
    });
    const LIST: OutgoingsResponse = {
      items: [
        pnbp(14, 21, "820261121000004", 1),
        pnbp(13, 20, "820261019000001", 2),
        pnbp(12, 19, "820261119000002", 1),
        pnbp(11, 18, "820261118000001", 1),
        DATA.items[1],
      ],
      by_kind: [],
      n: 5,
      total_idr: 0,
      n_justified: 0,
      justified_total_idr: 0,
    };
    const bar = () => screen.getByTestId("pnbp-batch");

    it("flags a code debited twice and leaves it out of the batch", async () => {
      api.getOutgoings.mockResolvedValue(LIST);
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-13");
      expect(screen.getByTestId("code-twice-13").textContent).toBe(
        "Paid twice?",
      );
      expect(screen.queryByTestId("code-twice-14")).toBeNull();
      expect(
        within(bar()).getByText(
          "1 PNBP looks paid twice: check it and justify it by hand.",
        ),
      ).toBeTruthy();
      // the newest single-code ones, up to the limit: 14 and 12, never 13
      expect(
        within(bar()).getByRole("button", { name: "Justify all PNBP (2)" }),
      ).toBeTruthy();
    });

    it("asks first, sends the ids, then offers Undo of that batch", async () => {
      api.getOutgoings.mockResolvedValue(LIST);
      api.justifyPnbpBatch.mockResolvedValue({
        batch_id: "b-1",
        n: 2,
        total_idr: 26000000,
        state: "justified",
      });
      api.undoOutgoingBatch.mockResolvedValue({
        batch_id: "b-1",
        n: 2,
        state: "to_justify",
      });
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-14");
      fireEvent.click(
        within(bar()).getByRole("button", { name: /Justify all PNBP/ }),
      );
      expect(
        within(bar()).getByText(/Mark the 2 PNBP payments as justified\?/),
      ).toBeTruthy();
      expect(api.justifyPnbpBatch).not.toHaveBeenCalled();
      fireEvent.click(
        within(bar()).getByRole("button", { name: "Yes, justify all" }),
      );
      await waitFor(() =>
        expect(api.justifyPnbpBatch).toHaveBeenCalledWith([14, 12]),
      );
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith("2 PNBP justified"),
      );
      const status = await within(bar()).findByRole("status");
      expect(status.textContent).toBe("2 PNBP justified. Undo");
      fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
      await waitFor(() =>
        expect(api.undoOutgoingBatch).toHaveBeenCalledWith("b-1"),
      );
      await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    });

    it("offers the button only where the PNBP are on screen", async () => {
      // Gate r1 on #616: with the Transfer filter the button sent PNBP the accountant could not see.
      api.getOutgoings.mockResolvedValue({
        ...LIST,
        by_kind: [
          { kind: "government_fee", label: "PNBP", n: 4, total_idr: 50000000 },
          {
            kind: "transfer_out",
            label: "Transfer",
            n: 1,
            total_idr: 12000000,
          },
        ],
      });
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-14");
      const filter = screen.getByRole("group", { name: "Filter by kind" });
      fireEvent.click(
        within(filter).getByRole("button", { name: /^Transfer/ }),
      );
      expect(screen.queryByTestId("pnbp-batch")).toBeNull();
      fireEvent.click(within(filter).getByRole("button", { name: /^PNBP/ }));
      expect(
        within(bar()).getByRole("button", { name: "Justify all PNBP (2)" }),
      ).toBeTruthy();
    });

    it("drops the Undo of a set once the Bali day is over", async () => {
      // Gate r1 on #616: a set made at 23:59 WITA still offered Undo at 00:01.
      vi.useFakeTimers({ toFake: ["Date"] });
      try {
        vi.setSystemTime(new Date("2026-11-22T15:59:00Z")); // 23:59 WITA
        api.getOutgoings.mockResolvedValue(LIST);
        api.justifyPnbpBatch.mockResolvedValue({
          batch_id: "b-1",
          n: 2,
          total_idr: 26000000,
          state: "justified",
        });
        render(<OutgoingsView />);
        await screen.findByTestId("outgoing-14");
        fireEvent.click(
          within(bar()).getByRole("button", { name: /Justify all PNBP/ }),
        );
        fireEvent.click(
          within(bar()).getByRole("button", { name: "Yes, justify all" }),
        );
        await within(bar()).findByRole("status");
        vi.setSystemTime(new Date("2026-11-22T16:01:00Z")); // 00:01 WITA, next day
        fireEvent.click(screen.getByRole("button", { name: /^Justified/ }));
        fireEvent.click(screen.getByRole("button", { name: /^To justify/ }));
        await screen.findByTestId("outgoing-14");
        expect(screen.queryByRole("status")).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it("Cancel sends nothing", async () => {
      api.getOutgoings.mockResolvedValue(LIST);
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-14");
      fireEvent.click(
        within(bar()).getByRole("button", { name: /Justify all PNBP/ }),
      );
      fireEvent.click(within(bar()).getByRole("button", { name: "Cancel" }));
      expect(
        within(bar()).getByRole("button", { name: /Justify all PNBP/ }),
      ).toBeTruthy();
      expect(api.justifyPnbpBatch).not.toHaveBeenCalled();
    });

    it("shows the server's refusal as is and keeps the question open", async () => {
      api.getOutgoings.mockResolvedValue(LIST);
      api.justifyPnbpBatch.mockRejectedValue(
        new Error("a debit is already justified: reload the list"),
      );
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-14");
      fireEvent.click(
        within(bar()).getByRole("button", { name: /Justify all PNBP/ }),
      );
      fireEvent.click(
        within(bar()).getByRole("button", { name: "Yes, justify all" }),
      );
      expect((await within(bar()).findByRole("alert")).textContent).toBe(
        "a debit is already justified: reload the list",
      );
      expect(
        within(bar()).getByRole("button", { name: "Yes, justify all" }),
      ).toBeTruthy();
    });

    it("offers no batch when no PNBP has a single code", async () => {
      api.getOutgoings.mockResolvedValue(DATA); // a reading without code_debits
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-1");
      expect(screen.queryByTestId("pnbp-batch")).toBeNull();
    });

    it("offers Undo on Justified only for a batch made today", async () => {
      const done = (id: number, batch: string, at: string) => ({
        ...pnbp(id, 19, `82026111900000${id}`, 1),
        state: "justified" as const,
        justification: {
          category: "pnbp_case" as const,
          practice_id: null,
          recipient: null,
          note: null,
          lines: [],
          by: "staff@example.test",
          at,
          batch_id: batch,
        },
      });
      api.getOutgoings.mockImplementation(async (opts) =>
        opts?.state === "justified"
          ? {
              ...JUSTIFIED,
              items: [
                done(1, "b-today", todayIso()),
                done(2, "b-today", todayIso()),
                done(3, "b-old", "2026-11-02T03:00:00+00:00"),
              ],
            }
          : DATA,
      );
      api.undoOutgoingBatch.mockResolvedValue({
        batch_id: "b-today",
        n: 2,
        state: "to_justify",
      });
      render(<OutgoingsView />);
      await screen.findByTestId("outgoing-1");
      fireEvent.click(screen.getByRole("button", { name: /^Justified/ }));
      const today = await screen.findByTestId("batch-b-today");
      expect(today.textContent).toMatch(/^2 PNBP justified together today ·/);
      expect(screen.queryByTestId("batch-b-old")).toBeNull();
      expect(screen.getByTestId("justification-1").textContent).toMatch(
        /Justified together/,
      );
      fireEvent.click(within(today).getByRole("button", { name: "Undo" }));
      await waitFor(() =>
        expect(api.undoOutgoingBatch).toHaveBeenCalledWith("b-today"),
      );
    });
  });
});
