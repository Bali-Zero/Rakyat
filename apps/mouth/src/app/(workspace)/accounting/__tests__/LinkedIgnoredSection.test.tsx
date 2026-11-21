import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
  act,
} from "@testing-library/react";
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

  it("B3: after a reopen the row leaves at once, no Loading, and a failed refresh does not bring it back", async () => {
    api.getBankTransactions.mockImplementation(
      async (opts) =>
        (opts?.reconciledStatus === "manual"
          ? [txn(1, "ROW ONE"), txn(2, "ROW TWO")]
          : []) as never,
    );
    api.reopenTransaction.mockResolvedValue({} as never);
    const runWrite = async (fn: () => Promise<unknown>) => {
      await fn();
      return true;
    };
    const props = { locked: false, exclusive: runWrite, onReopened: vi.fn() };
    const { rerender } = render(
      <LinkedIgnoredSection version={0} {...props} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW ONE");

    // the refresh that follows the reopen is held, then rejects
    let rejectRefresh!: (e: Error) => void;
    api.getBankTransactions.mockImplementation(
      () =>
        new Promise((_, rej) => {
          rejectRefresh = rej;
        }) as never,
    );
    const rowOne = screen
      .getByText("ROW ONE")
      .closest("div.rounded-md") as HTMLElement;
    fireEvent.click(within(rowOne).getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(screen.queryByText("ROW ONE")).toBeNull());
    expect(screen.getByText("ROW TWO")).toBeInTheDocument();

    rerender(<LinkedIgnoredSection version={1} {...props} />); // refresh starts
    await waitFor(() => expect(api.getBankTransactions).toHaveBeenCalled());
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.getByText("ROW TWO")).toBeInTheDocument();

    rejectRefresh(new Error("down"));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.queryByText("ROW ONE")).toBeNull();
    expect(screen.getByText("ROW TWO")).toBeInTheDocument();
  });

  it("F3: an older refresh answering right after a reopen never brings the row back", async () => {
    const calls: Array<ReturnType<typeof deferred<unknown[]>>> = [];
    let phase = "first";
    api.getBankTransactions.mockImplementation(async (opts) => {
      if (phase === "first")
        return (
          opts?.reconciledStatus === "manual"
            ? [txn(1, "ROW ONE"), txn(2, "ROW TWO")]
            : []
        ) as never;
      const d = deferred<unknown[]>();
      calls.push(d);
      return d.promise as never;
    });
    const reopenHeld = deferred<unknown>();
    api.reopenTransaction.mockReturnValue(reopenHeld.promise as never);
    const runWrite = async (fn: () => Promise<unknown>) => {
      await fn();
      return true;
    };
    const props = { locked: false, exclusive: runWrite, onReopened: vi.fn() };
    const { rerender } = render(
      <LinkedIgnoredSection version={0} {...props} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    await screen.findByText("ROW ONE");

    phase = "later";
    rerender(<LinkedIgnoredSection version={1} {...props} />); // the older refresh is in flight
    await waitFor(() => expect(calls.length).toBe(2));
    const rowOne = screen
      .getByText("ROW ONE")
      .closest("div.rounded-md") as HTMLElement;
    fireEvent.click(within(rowOne).getByRole("button", { name: "Reopen" }));

    await act(async () => {
      reopenHeld.resolve({});
      await Promise.resolve();
      calls[0].resolve([txn(1, "ROW ONE"), txn(2, "ROW TWO")]); // older answers first
      calls[1].resolve([]);
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("ROW ONE")).toBeNull();
  });

  it("F7: a failed first load is not an empty list; Try again loads; a successful empty load says so", async () => {
    api.getBankTransactions.mockRejectedValue(new Error("down"));
    const props = {
      locked: false,
      exclusive: async (f: () => Promise<unknown>) => {
        await f();
        return true;
      },
      onReopened: vi.fn(),
    };
    render(<LinkedIgnoredSection version={0} {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Linked and ignored" }));
    expect(
      await screen.findByText("Could not load this list."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Nothing linked or ignored yet.")).toBeNull();
    api.getBankTransactions.mockResolvedValue([]);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Nothing linked or ignored yet.");
    expect(screen.queryByText("Could not load this list.")).toBeNull();
  });

  describe("N1: the empty message follows the load result", () => {
    const runWrite = async (fn: () => Promise<unknown>) => {
      await fn();
      return true;
    };
    const props = { locked: false, exclusive: runWrite, onReopened: vi.fn() };
    const lastRow = async () => {
      api.getBankTransactions.mockImplementation(
        async (o) =>
          (o?.reconciledStatus === "manual"
            ? [txn(1, "ROW ONE")]
            : []) as never,
      );
      api.reopenTransaction.mockResolvedValue({} as never);
      const view = render(<LinkedIgnoredSection version={0} {...props} />);
      fireEvent.click(
        screen.getByRole("button", { name: "Linked and ignored" }),
      );
      await screen.findByText("ROW ONE");
      return view;
    };
    const reopenLast = () =>
      fireEvent.click(screen.getByRole("button", { name: "Reopen" }));

    it("last row reopened, refresh held: loading line; then empty / failure by the result", async () => {
      await lastRow();
      const calls: Array<ReturnType<typeof deferred<unknown[]>>> = [];
      api.getBankTransactions.mockImplementation(() => {
        const d = deferred<unknown[]>();
        calls.push(d);
        return d.promise as never;
      });
      reopenLast();
      await screen.findByText("Loading…");
      expect(screen.queryByText("Nothing linked or ignored yet.")).toBeNull();
      expect(screen.queryByText("Could not load this list.")).toBeNull();
      await act(async () => {
        calls[0].resolve([]);
        calls[1].resolve([]);
      });
      await screen.findByText("Nothing linked or ignored yet.");
    });

    it("the held refresh fails: failure text; a new load in flight with no rows: loading line", async () => {
      await lastRow();
      let rej!: (e: Error) => void;
      api.getBankTransactions.mockImplementation(
        () =>
          new Promise((_, r) => {
            rej = r;
          }) as never,
      );
      reopenLast();
      await screen.findByText("Loading…");
      await act(async () => {
        rej(new Error("down"));
      });
      await screen.findByText("Could not load this list.");
      api.getBankTransactions.mockImplementation(
        () => new Promise(() => {}) as never,
      );
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await screen.findByText("Loading…");
      expect(screen.queryByText("Could not load this list.")).toBeNull();
    });
  });
});
