import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { LinkedIgnoredSection } from "../_components/LinkedIgnoredSection";
import { accountingApi } from "@/lib/api/workspace/accounting.api";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: { getBankTransactions: vi.fn(), reopenTransaction: vi.fn() },
}));

const api = vi.mocked(accountingApi);
const txn = (id: number, description: string) =>
  ({
    id,
    description,
    txn_date: "2026-01-02",
    amount_idr: 1,
    reconciled_status: "manual",
    direction: "credit",
  }) as never;

const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

beforeEach(() => vi.clearAllMocks());

describe("LinkedIgnoredSection", () => {
  it("G3: an older refresh answering after a newer one never replaces the list", async () => {
    const calls: Array<ReturnType<typeof deferred<unknown[]>>> = [];
    api.getBankTransactions.mockImplementation(() => {
      const d = deferred<unknown[]>();
      calls.push(d);
      return d.promise as never;
    });
    const props = { locked: false, exclusive: vi.fn(), onReopened: vi.fn() };
    const { rerender } = render(
      <LinkedIgnoredSection version={0} {...props} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await waitFor(() => expect(calls.length).toBe(2)); // older refresh: manual + ignored
    rerender(<LinkedIgnoredSection version={1} {...props} />);
    await waitFor(() => expect(calls.length).toBe(4)); // newer refresh

    calls[2].resolve([txn(2, "ROW NEWER")]);
    calls[3].resolve([]);
    await screen.findByText("ROW NEWER");
    calls[0].resolve([txn(1, "ROW OLDER")]);
    calls[1].resolve([]);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("ROW OLDER")).toBeNull();
    expect(screen.getByText("ROW NEWER")).toBeInTheDocument();
  });
});
