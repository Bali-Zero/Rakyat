/**
 * BankStatementUpload: two-step preview then import of the SAME File.
 * All names, accounts and amounts are invented.
 */

import {
  render,
  screen,
  waitFor,
  fireEvent,
  act,
} from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { BankStatementUpload } from "../_components/BankStatementUpload";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { importFixture, previewFixture } from "./fixtures";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
const toastError = toast.error;
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    previewBankStatement: vi.fn(),
    importBankStatement: vi.fn(),
  },
}));

const preview = vi.mocked(accountingApi.previewBankStatement);
const doImport = vi.mocked(accountingApi.importBankStatement);

const csv = () =>
  new File(["contoh"], "statement-0000000000.csv", { type: "text/csv" });
const input = () => screen.getByTestId("bank-statement-input");
const choose = (f: File) =>
  fireEvent.change(input(), { target: { files: [f] } });

const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("BankStatementUpload", () => {
  it("U1: preview on choose; import is not called until the user clicks Import", async () => {
    preview.mockResolvedValue(previewFixture());
    render(<BankStatementUpload onImported={vi.fn()} />);
    expect(screen.getByLabelText(/upload bank statement \(csv\)/i)).toBe(
      input(),
    );
    const f = csv();
    choose(f);

    await screen.findByRole("button", { name: /import 3 transactions/i });
    expect(preview).toHaveBeenCalledTimes(1);
    expect(doImport).not.toHaveBeenCalled();

    expect(screen.getByText(/0000000000 · CONTOH PT/)).toBeInTheDocument();
    expect(screen.getByText("2026-01-02 to 2026-01-30")).toBeInTheDocument();
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("transfer in")).toBeInTheDocument();
    expect(doImport).not.toHaveBeenCalled();
  });

  it("shows amounts with cents, the period without T, and kinds with spaces", async () => {
    preview.mockResolvedValue(
      previewFixture({
        period: { first: "2026-11-01T19:31:25", last: "2026-11-17T15:31:06" },
        opening_balance: "361900825.55",
        closing_balance: "1500000.00",
        kinds: { government_fee: { count: 1, total: "250000.50" } },
      }),
    );
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    await screen.findByText("Rp 361.900.825,55");
    expect(screen.getByText("Rp 1.500.000")).toBeInTheDocument();
    expect(screen.getByText("Rp 250.000,50")).toBeInTheDocument();
    expect(
      screen.getByText("2026-11-01 19:31:25 to 2026-11-17 15:31:06"),
    ).toBeInTheDocument();
    expect(screen.getByText("government fee")).toBeInTheDocument();
  });

  it("U2: Import is disabled and a warning shows when the balance check fails", async () => {
    preview.mockResolvedValue(
      previewFixture({
        balance_check: { ok: false, mismatch_positions: [2, 5] },
      }),
    );
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());

    const btn = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    expect(btn).toBeDisabled();
    expect(screen.getByText("2 mismatches")).toBeInTheDocument();
    expect(screen.getByText(/running balance does not add up/i)).toBeVisible();
    fireEvent.click(btn);
    expect(doImport).not.toHaveBeenCalled();
  });

  it("U3: import receives the very File object that was previewed, then refreshes", async () => {
    preview.mockResolvedValue(previewFixture());
    doImport.mockResolvedValue(
      importFixture({ rows_inserted: 2, rows_duplicate: 1 }),
    );
    const onImported = vi.fn();
    render(<BankStatementUpload onImported={onImported} />);
    const f = csv();
    choose(f);

    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    await screen.findByText("2 new transactions stored, 1 already stored.");

    expect(doImport).toHaveBeenCalledTimes(1);
    expect(doImport.mock.calls[0][0]).toBe(f);
    expect(preview.mock.calls[0][0]).toBe(f);
    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("shows the nothing-new line and the segments note", async () => {
    preview.mockResolvedValue(previewFixture());
    doImport.mockResolvedValue(
      importFixture({
        nothing_new: true,
        rows_inserted: 0,
        history_segments: 2,
      }),
    );
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    await screen.findByText(
      "Nothing new: every transaction of this statement was already stored.",
    );
    expect(
      screen.getByText(
        /The stored history of this account has 2 separate segments: a statement for a period in between has not been uploaded yet\./,
      ),
    ).toBeInTheDocument();
  });

  it("U4: a preview error shows the server text, toasts, and re-enables the input", async () => {
    preview.mockRejectedValue(new Error("not a bank statement"));
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "not a bank statement",
    );
    expect(toastError).toHaveBeenCalledWith(
      "Statement preview failed",
      "not a bank statement",
    );
    expect(input()).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /import/i }),
    ).not.toBeInTheDocument();
  });

  it("U4: an import error shows the server text and allows choosing a file again", async () => {
    preview.mockResolvedValue(previewFixture());
    doImport.mockRejectedValue(new Error("bank statement could not be stored"));
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "bank statement could not be stored",
      ),
    );
    expect(input()).toBeEnabled();
    preview.mockResolvedValue(previewFixture({ row_count: 9 }));
    choose(csv());
    await screen.findByRole("button", { name: /import 9 transactions/i });
  });

  it("U5: a second click while the import is pending sends one request; controls are disabled", async () => {
    preview.mockResolvedValue(previewFixture());
    const d = deferred<ReturnType<typeof importFixture>>();
    doImport.mockReturnValue(d.promise);
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    const btn = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);

    expect(doImport).toHaveBeenCalledTimes(1);
    expect(btn).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(input()).toBeDisabled();

    d.resolve(importFixture());
    await screen.findByText("3 new transactions stored, 0 already stored.");
    expect(doImport).toHaveBeenCalledTimes(1);
    expect(input()).toBeEnabled();
  });

  it("choosing a new file discards the previous result; Cancel discards the preview", async () => {
    preview.mockResolvedValue(previewFixture());
    doImport.mockResolvedValue(importFixture());
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    fireEvent.click(
      await screen.findByRole("button", { name: /import 3 transactions/i }),
    );
    await screen.findByText(/new transactions stored/);

    const d = deferred<ReturnType<typeof previewFixture>>();
    preview.mockReturnValue(d.promise);
    choose(csv());
    await waitFor(() =>
      expect(screen.queryByText(/new transactions stored/)).toBeNull(),
    );
    d.resolve(previewFixture({ row_count: 4 }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("CONTOH PT", { exact: false })).toBeNull();
  });

  it("F4: Cancel in the same tick as Import does not reset the card while the import is in flight", async () => {
    preview.mockResolvedValue(previewFixture());
    const d = deferred<ReturnType<typeof importFixture>>();
    doImport.mockReturnValue(d.promise);
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    const importBtn = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      importBtn.click();
      cancel.click();
    });
    expect(doImport).toHaveBeenCalledTimes(1);
    expect(input()).toBeDisabled();
    expect(screen.getByText(/0000000000 · CONTOH PT/)).toBeInTheDocument();
    d.resolve(importFixture());
    await screen.findByText(/new transactions stored/);
  });

  it("N3a: Cancel then Import in one tick imports nothing", async () => {
    preview.mockResolvedValue(previewFixture());
    render(<BankStatementUpload onImported={vi.fn()} />);
    choose(csv());
    const importBtn = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    act(() => {
      cancel.click();
      importBtn.click();
    });
    expect(doImport).not.toHaveBeenCalled();
    expect(input()).toBeEnabled();
  });

  it("81: the upload panel exists only for a phase, message, report or outcome", async () => {
    const heldPreview = deferred<ReturnType<typeof previewFixture>>();
    preview.mockReturnValueOnce(heldPreview.promise);
    render(<BankStatementUpload onImported={vi.fn()} />);
    expect(screen.queryByRole("region", { name: "Bank statement" })).toBeNull();

    choose(csv());
    const previewPanel = await screen.findByRole("region", {
      name: "Bank statement",
    });
    expect(previewPanel).toHaveTextContent("Reading statement…");

    await act(async () => heldPreview.reject(new Error("preview message")));
    expect(screen.getByRole("alert")).toHaveTextContent("preview message");
    expect(
      screen.getByRole("region", { name: "Bank statement" }),
    ).toBeInTheDocument();

    preview.mockResolvedValueOnce(previewFixture());
    choose(csv());
    const importButton = await screen.findByRole("button", {
      name: /import 3 transactions/i,
    });
    expect(
      screen.getByRole("region", { name: "Bank statement" }),
    ).toHaveTextContent("CONTOH PT");

    doImport.mockResolvedValueOnce(importFixture());
    fireEvent.click(importButton);
    await screen.findByText("3 new transactions stored, 0 already stored.");
    expect(
      screen.getByRole("region", { name: "Bank statement" }),
    ).toBeInTheDocument();
  });
});
