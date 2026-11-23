import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CashReceiptForm } from "../_components/CashReceiptForm";
import { accountingApi } from "@/lib/api/workspace/accounting.api";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("../_components/CompanyStrip", () => ({
  useCompanies: () => ({ status: "ok", rows: [] }),
  CompanyStrip: ({ onSelect }: { onSelect: (key: string) => void }) => (
    <button onClick={() => onSelect("bnk")}>BNK</button>
  ),
}));
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { getCashInvoices: vi.fn(), createCashReceipt: vi.fn() },
}));
const api = vi.mocked(accountingApi);
let ids = 0;

beforeEach(() => {
  vi.resetAllMocks();
  ids = 0;
  vi.stubGlobal("crypto", { randomUUID: () => `uuid-${++ids}` });
  api.getCashInvoices.mockResolvedValue({
    items: [
      {
        invoice_id: 1,
        invoice_number: "INV-1",
        practice_id: 2,
        client_name: "Client",
        service_name: "Visa",
        amount_idr: 100,
        outstanding_idr: 100,
      },
    ],
  });
});

async function ready() {
  render(<CashReceiptForm onDone={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "BNK" }));
  fireEvent.change(screen.getByLabelText("Find invoice"), {
    target: { value: "INV" },
  });
  fireEvent.click(await screen.findByText(/INV-1/));
}

describe("CashReceiptForm", () => {
  it("keeps the UUID and form data for an ambiguous retry, then reports the receipt", async () => {
    await ready();
    api.createCashReceipt.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(
      screen.getByRole("button", { name: "Record cash receipt" }),
    );
    await waitFor(() => expect(api.createCashReceipt).toHaveBeenCalledTimes(1));
    api.createCashReceipt.mockResolvedValueOnce({
      cashout_id: 9,
      status_after: "partial",
      outstanding_idr: 50,
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Record cash receipt" }),
    );
    await waitFor(() => expect(api.createCashReceipt).toHaveBeenCalledTimes(2));
    expect(api.createCashReceipt.mock.calls[0][0].request_id).toBe(
      api.createCashReceipt.mock.calls[1][0].request_id,
    );
    expect(toast.success).toHaveBeenCalled();
  });

  it("generates a fresh UUID after a payload edit and prevents rapid duplicate submits", async () => {
    await ready();
    let resolve!: (v: any) => void;
    api.createCashReceipt.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }) as any,
    );
    const submit = screen.getByRole("button", { name: "Record cash receipt" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(api.createCashReceipt).toHaveBeenCalledTimes(1);
    const first = api.createCashReceipt.mock.calls[0][0].request_id;
    resolve({ cashout_id: 9, status_after: "partial", outstanding_idr: 50 });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Amount IDR"), {
      target: { value: "50" },
    });
    api.createCashReceipt.mockResolvedValueOnce({
      cashout_id: 10,
      status_after: "partial",
      outstanding_idr: 0,
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Record cash receipt" }),
    );
    await waitFor(() => expect(api.createCashReceipt).toHaveBeenCalledTimes(2));
    expect(api.createCashReceipt.mock.calls[1][0].request_id).not.toBe(first);
  });
});
