/**
 * Outgoings to justify (owner 2026-11-21): the debits nobody has justified, read only.
 * Names, codes and amounts are invented.
 */

import { render, screen, fireEvent, within } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { OutgoingsView } from "../_components/OutgoingsView";
import {
  accountingApi,
  type OutgoingsResponse,
} from "@/lib/api/workspace/accounting.api";

vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { getOutgoings: vi.fn() },
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
    },
  ],
  by_kind: [
    { kind: "transfer_out", label: "Transfer", n: 1, total_idr: 12000000 },
    { kind: "government_fee", label: "PNBP", n: 1, total_idr: 5000000 },
    { kind: "transfer_fee", label: "Transfer fee", n: 1, total_idr: 2500 },
  ],
  n: 3,
  total_idr: 17002500,
};

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
    expect(api.getOutgoings).toHaveBeenCalledWith();
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
    expect(
      screen.getByText(
        /Justifying them comes next: this page changes nothing\./,
      ),
    ).toBeInTheDocument();
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
});
