/**
 * Multipart client for the cashout PDF and the bank statement routes (fetch mocked).
 */

import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";

const mockApi = vi.hoisted(() => ({
  getBaseUrl: vi.fn(() => "https://api.test"),
  getToken: vi.fn<() => string | null>(() => "tok-123"),
  getCsrfToken: vi.fn<() => string | null>(() => "csrf-456"),
  get: vi.fn(),
  post: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: mockApi }));

import { accountingApi } from "./accounting.api";

const file = new File(["contoh"], "contoh.csv", { type: "text/csv" });
const fetchMock = vi.fn();

const ok = (body: unknown) =>
  ({ ok: true, json: async () => body }) as unknown as Response;
const fail = (status: number, statusText: string, body?: unknown) =>
  ({
    ok: false,
    status,
    statusText,
    json: async () => {
      if (body === undefined) throw new SyntaxError("not json");
      return body;
    },
  }) as unknown as Response;

beforeEach(() => {
  fetchMock.mockReset();
  mockApi.getToken.mockReturnValue("tok-123");
  mockApi.getCsrfToken.mockReturnValue("csrf-456");
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe.each([
  ["previewBankStatement", "/api/crm/accounting/bank-statement/preview"],
  ["importBankStatement", "/api/crm/accounting/bank-statement/import"],
  ["importCashout", "/api/crm/accounting/import-cashout"],
] as const)("%s", (method, path) => {
  it("POSTs multipart field `file` with credentials and auth headers", async () => {
    fetchMock.mockResolvedValue(ok({ fine: true }));
    const res = await accountingApi[method](file);

    expect(res).toEqual({ fine: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`https://api.test${path}`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(init.headers).toEqual({
      "X-CSRF-Token": "csrf-456",
      Authorization: "Bearer tok-123",
    });
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.body.get("file")).toBe(file);
    expect([...init.body.keys()]).toEqual(["file"]);
  });

  it("omits the headers when there is no token or CSRF", async () => {
    mockApi.getToken.mockReturnValue(null);
    mockApi.getCsrfToken.mockReturnValue(null);
    fetchMock.mockResolvedValue(ok({}));
    await accountingApi[method](file);
    expect(fetchMock.mock.calls[0][1].headers).toEqual({});
  });

  it("surfaces the backend `detail` on 422", async () => {
    fetchMock.mockResolvedValue(
      fail(422, "Unprocessable Entity", { detail: "file is empty" }),
    );
    await expect(accountingApi[method](file)).rejects.toThrow("file is empty");
  });

  it("falls back to statusText on a non-JSON error body", async () => {
    fetchMock.mockResolvedValue(fail(413, "Payload Too Large"));
    await expect(accountingApi[method](file)).rejects.toThrow(
      "Payload Too Large",
    );
  });
});
