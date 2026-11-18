/**
 * getDoneToday through the real api client with a mocked fetch: URL, method,
 * no query string, and the statusCode of a 404.
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

const body = {
  day: "2026-11-18",
  timezone: "Asia/Makassar",
  counts: { confirmed: 0, linked: 0, ignored: 0, total: 0 },
  items: [],
  truncated: false,
};

describe("getDoneToday", () => {
  it("GETs /done-today with no query string and returns the body", async () => {
    fetchMock.mockResolvedValue(json(200, body));
    await expect(accountingApi.getDoneToday()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url).endsWith("/api/crm/accounting/done-today")).toBe(true);
    expect(String(url)).not.toContain("?");
    expect(((init as RequestInit).method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("rejects a 404 with statusCode 404", async () => {
    fetchMock.mockResolvedValue(json(404, { detail: "Not Found" }));
    await expect(accountingApi.getDoneToday()).rejects.toMatchObject({
      statusCode: 404,
    });
  });
});
