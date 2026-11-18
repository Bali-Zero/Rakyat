/**
 * JSON calls of the reconciliation lights, through the real api client with a
 * mocked fetch: URL, method, body, and the backend `detail` surfaced on 422.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { accountingApi } from "./accounting.api";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const call = () => {
  const [url, init] = fetchMock.mock.calls[0];
  return { url: String(url), init: init as RequestInit };
};

describe("reconciliation lights API", () => {
  it("getReconciliationInbox GETs the inbox with the limit", async () => {
    fetchMock.mockResolvedValue(json(200, []));
    await expect(accountingApi.getReconciliationInbox(50)).resolves.toEqual([]);
    const { url, init } = call();
    expect(url).toContain("/api/crm/accounting/reconciliation-inbox?limit=50");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("getReconciliationInbox without a limit sends no query", async () => {
    fetchMock.mockResolvedValue(json(200, []));
    await accountingApi.getReconciliationInbox();
    expect(
      call().url.endsWith("/api/crm/accounting/reconciliation-inbox"),
    ).toBe(true);
  });

  it("linkRecorded POSTs the three ids", async () => {
    fetchMock.mockResolvedValue(
      json(200, {
        bank_txn_id: 7,
        reconciled_status: "manual",
        reconciliation_log_id: 1,
      }),
    );
    await accountingApi.linkRecorded({
      bank_txn_id: 7,
      practice_id: 3,
      invoice_id: 9,
    });
    const { url, init } = call();
    expect(url).toContain("/api/crm/accounting/link-recorded");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      bank_txn_id: 7,
      practice_id: 3,
      invoice_id: 9,
    });
  });

  it("ignoreTransaction POSTs the id and the reason", async () => {
    fetchMock.mockResolvedValue(
      json(200, {
        bank_txn_id: 7,
        reconciled_status: "ignored",
        reconciliation_log_id: 2,
      }),
    );
    await accountingApi.ignoreTransaction({
      bank_txn_id: 7,
      reason: "own account transfer",
    });
    const { url, init } = call();
    expect(url).toContain("/api/crm/accounting/ignore-transaction");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      bank_txn_id: 7,
      reason: "own account transfer",
    });
  });

  it("reopenTransaction POSTs only the id", async () => {
    fetchMock.mockResolvedValue(
      json(200, {
        bank_txn_id: 7,
        reconciled_status: "unmatched",
        reconciliation_log_id: 3,
      }),
    );
    await accountingApi.reopenTransaction(7);
    const { url, init } = call();
    expect(url).toContain("/api/crm/accounting/reopen-transaction");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ bank_txn_id: 7 });
  });

  it.each([
    [
      "linkRecorded",
      () =>
        accountingApi.linkRecorded({
          bank_txn_id: 1,
          practice_id: 1,
          invoice_id: 1,
        }),
    ],
    [
      "ignoreTransaction",
      () => accountingApi.ignoreTransaction({ bank_txn_id: 1, reason: "abc" }),
    ],
    ["reopenTransaction", () => accountingApi.reopenTransaction(1)],
  ])("%s surfaces the fixed-text detail on 422", async (_n, run) => {
    fetchMock.mockResolvedValue(
      json(422, { detail: "fixed text from server" }),
    );
    await expect(run()).rejects.toThrow("fixed text from server");
  });
});
