/**
 * The confirm dialog names the invoice and states what confirming does. All values invented.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { ConfirmModal } from "../_components/ConfirmModal";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { candidate } from "./fixtures";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { confirm: vi.fn() },
}));

const STATEMENT =
  "Confirming records this payment on the practice, the invoice and the cashout. You can undo it today from Done today. Paid in full, a case waiting for payment moves to On Process.";

const full = {
  invoice_number: "INV-000000-00001",
  invoice_issued_at: "2026-01-10T00:00:00+00:00",
  service_name: "Example service",
};

beforeEach(() => vi.clearAllMocks());

function renderModal(
  over: Parameters<typeof candidate>[0] = {},
  amount = 500000,
) {
  return render(
    <ConfirmModal
      txn={{
        id: 1,
        amount_idr: amount,
        description: "TRF CONTOH",
        txn_date: "2026-01-15",
      }}
      candidate={candidate(over)}
      onClose={vi.fn()}
      onConfirmed={vi.fn()}
    />,
  );
}

const confirmButton = () => screen.getByRole("button", { name: /^Confirm / });
const labelled = (label: string) =>
  screen.queryByText(label, { selector: "dt" })?.nextElementSibling ?? null;

describe("dialog identity lines", () => {
  it("shows Invoice no., Issued and Service as separate labelled lines", () => {
    renderModal(full);
    expect(labelled("Invoice no.")).toHaveTextContent("INV-000000-00001");
    expect(labelled("Issued")).toHaveTextContent("2026-01-10");
    expect(labelled("Service")).toHaveTextContent("Example service");
    expect(screen.getByText("Invoice no.")).toHaveClass("whitespace-nowrap");
  });

  it.each([
    [
      "null",
      { invoice_number: null, invoice_issued_at: null, service_name: null },
    ],
    ["absent", {}],
    [
      "wrong types",
      { invoice_number: 7, invoice_issued_at: {}, service_name: 3 },
    ],
  ])(
    "%s values: No invoice number, and no Issued or Service line",
    (_n, over) => {
      renderModal(over as never);
      expect(labelled("Invoice no.")).toHaveTextContent(/^No invoice number$/);
      expect(labelled("Issued")).toBeNull();
      expect(labelled("Service")).toBeNull();
    },
  );

  it("keeps the existing Invoice amount line", () => {
    renderModal(full);
    expect(labelled("Invoice")).not.toBeNull();
    expect(labelled("Delta")).not.toBeNull();
  });
});

describe("the statement, in every status the dialog can present", () => {
  // newStatus is "paid" or "partial" (ConfirmModal.tsx): exact, net of withholding, short.
  const cases: [string, Parameters<typeof candidate>[0], number, string][] = [
    ["paid, exact", {}, 500000, "Confirm paid"],
    [
      "paid, net of withholding",
      { likely_withheld: true, invoice_amount_idr: 600000 },
      500000,
      "Confirm paid",
    ],
    [
      "partial, short and not withholding",
      { invoice_amount_idr: 900000 },
      500000,
      "Confirm partial",
    ],
  ];

  it.each(cases)(
    "%s: verbatim, before the button, not hidden",
    (_n, over, amount, button) => {
      renderModal(over, amount);
      expect(screen.getByRole("button", { name: button })).toBeInTheDocument();
      const statement = screen.getByText(
        (_c, el) =>
          el?.tagName === "P" &&
          el.textContent?.replace(/\s+/g, " ").trim() === STATEMENT,
      );
      expect(
        statement.compareDocumentPosition(confirmButton()) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      const panel = screen
        .getByText("Confirm payment match")
        .closest(".max-w-md");
      expect(panel).not.toBeNull();
      for (
        let el: Element | null = statement;
        el;
        el = el === panel ? null : el.parentElement
      ) {
        expect(el.hasAttribute("hidden")).toBe(false);
        const style = (el as HTMLElement).style;
        expect(style.display).not.toBe("none");
        expect(style.visibility).not.toBe("hidden");
        expect(el.getAttribute("aria-hidden")).not.toBe("true");
        for (const c of ["hidden", "sr-only", "invisible"])
          expect(el.classList.contains(c)).toBe(false);
      }
    },
  );
});

describe("the success toast", () => {
  it("names the invoice number when there is one", async () => {
    vi.mocked(accountingApi.confirm).mockResolvedValue({} as never);
    renderModal(full);
    fireEvent.click(confirmButton());
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Payment confirmed",
        "INV-000000-00001 · CONTOH PT marked paid",
      ),
    );
  });

  it.each([
    ["absent", {}],
    ["whitespace only", { invoice_number: "   " }],
  ])("is unchanged when the number is %s", async (_n, over) => {
    vi.mocked(accountingApi.confirm).mockResolvedValue({} as never);
    renderModal(over);
    fireEvent.click(confirmButton());
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "Payment confirmed",
        "CONTOH PT marked paid",
      ),
    );
  });
});

describe("dialog identity, odd values", () => {
  const issuedLine = () => screen.queryByText("Issued", { selector: "dt" });

  it("a whitespace-only number shows No invoice number and no blank line", () => {
    renderModal({ invoice_number: "   " });
    expect(labelled("Invoice no.")).toHaveTextContent(/^No invoice number$/);
    for (const dd of document.querySelectorAll("dd"))
      expect(dd.textContent?.trim()).not.toBe("");
  });

  it("a date that does not begin as YYYY-MM-DD shows no Issued line", () => {
    renderModal({ invoice_issued_at: "n/a" });
    expect(issuedLine()).toBeNull();
  });

  it("an array that looks like a date once stringified shows no Issued line", () => {
    renderModal({ invoice_issued_at: ["2026-01-10"] as never });
    expect(issuedLine()).toBeNull();
  });
});

describe("the dialog keeps the lines it showed on the base", () => {
  it("shows Transfer, Client, Invoice, Delta, New status and every reason", () => {
    renderModal({
      reasons: ["first reason", "second reason"],
      client_name: "PT CONTOH",
    });
    for (const label of [
      "Transfer",
      "Client",
      "Invoice",
      "Delta",
      "New status",
    ])
      expect(screen.getByText(label, { selector: "dt" })).toBeInTheDocument();
    expect(labelled("Client")).toHaveTextContent("PT CONTOH");
    expect(screen.getByText("• first reason")).toBeInTheDocument();
    expect(screen.getByText("• second reason")).toBeInTheDocument();
  });
});

describe("the transfer the dialog records: amount, date and memo of THAT transfer", () => {
  // Two transfers of the same amount, distinguishable only by date and memo.
  const first = {
    id: 11,
    amount_idr: 2415000,
    description: "TRF CONTOH SATU INV-000000-00001",
    txn_date: "2026-01-18",
  };
  const second = {
    id: 12,
    amount_idr: 2415000,
    description: "TRF CONTOH DUA INV-000000-00002",
    txn_date: "2026-01-17",
  };

  function renderFor(txn: typeof first, over = {}) {
    return render(
      <ConfirmModal
        txn={txn}
        candidate={candidate({ invoice_amount_idr: 2415000, ...over })}
        onClose={vi.fn()}
        onConfirmed={vi.fn()}
      />,
    );
  }

  it("shows the date and the memo next to the amount, labelled (guilt for the base dialog)", () => {
    renderFor(second);
    expect(labelled("Transfer date")).toHaveTextContent(/^2026-01-17$/);
    expect(labelled("Memo")).toHaveTextContent(
      /^TRF CONTOH DUA INV-000000-00002$/,
    );
    // Right after the amount and before the client and the invoice number.
    const order = Array.from(document.querySelectorAll("dt")).map(
      (dt) => dt.textContent,
    );
    expect(order.slice(0, 4)).toEqual([
      "Transfer",
      "Transfer date",
      "Memo",
      "Client",
    ]);
  });

  it("of two transfers with the same amount, shows the chosen one's and none of the other's", () => {
    renderFor(second);
    const text = document.body.textContent ?? "";
    expect(text).toContain(second.description);
    expect(text).toContain(second.txn_date);
    expect(text).not.toContain(first.description);
    expect(text).not.toContain(first.txn_date);
  });

  it("confirming sends that transfer's id, amount, memo and date, once", async () => {
    vi.mocked(accountingApi.confirm).mockResolvedValue({} as never);
    renderFor(second, { practice_id: 202, invoice_id: 302 });
    fireEvent.click(confirmButton());
    await waitFor(() => expect(accountingApi.confirm).toHaveBeenCalledTimes(1));
    expect(accountingApi.confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        bank_txn_id: 12,
        practice_id: 202,
        invoice_id: 302,
        amount_applied_idr: 2415000,
        payment_reference: "TRF CONTOH DUA INV-000000-00002",
        movement_date: "2026-01-17",
      }),
    );
  });

  it.each([
    ["empty", ""],
    ["null", null],
  ])("a %s memo reads (no memo), as in the inbox row", (_n, description) => {
    renderFor({ ...second, description: description as never });
    expect(labelled("Memo")).toHaveTextContent(/^\(no memo\)$/);
  });
});

describe("an instalment against what is still owed", () => {
  const partly = {
    invoice_amount_idr: 10000000,
    paid_idr: 6000000,
    balance_idr: 4000000,
  };

  it("a 4M transfer completing a 10M invoice with 6M paid reads Confirm paid", () => {
    renderModal(partly, 4000000);
    expect(confirmButton()).toHaveTextContent("Confirm paid");
    expect(labelled("Already paid")).toHaveTextContent("Rp 6.000.000");
    expect(labelled("Remaining")).toHaveTextContent("Rp 4.000.000");
    expect(labelled("Delta")).toHaveTextContent(/^\+Rp 0$/);
  });

  it("a 3M transfer against the 4M balance is still partial", () => {
    renderModal(partly, 3000000);
    expect(confirmButton()).toHaveTextContent("Confirm partial");
  });

  it("with nothing paid there are no Already paid / Remaining lines", () => {
    renderModal({}, 500000);
    expect(labelled("Already paid")).toBeNull();
    expect(labelled("Remaining")).toBeNull();
    expect(confirmButton()).toHaveTextContent("Confirm paid");
  });

  it("an older backend (no balance field) is judged against the invoice amount", () => {
    renderModal({ invoice_amount_idr: 10000000 }, 4000000);
    expect(confirmButton()).toHaveTextContent("Confirm partial");
  });
});
