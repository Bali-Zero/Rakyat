import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { ApiClientBase } from "./client";
import { PublicAuthClient } from "./public-auth";
import { tokenFor } from "@/lib/auth/session-test-utils";

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sentAuthorization(
  spy: ReturnType<typeof vi.spyOn>,
): string | undefined {
  const init = spy.mock.calls[0][1] as { headers: Record<string, string> };
  return init.headers["Authorization"];
}

describe("requestWithCredential", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("reports the exact bearer placed in the Authorization header, and its sub", async () => {
    const token = tokenFor("42");
    localStorage.setItem("auth_token", token);
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({ a: 1 }));
    const client = new ApiClientBase("https://api.test");

    const res = await client.requestWithCredential<{ a: number }>("/api/x");

    expect(res.data).toEqual({ a: 1 });
    expect(res.sentToken).toBe(token);
    expect(res.sentSub).toBe("42");
    expect(sentAuthorization(spy)).toBe(`Bearer ${token}`);
  });

  it("reports null for a request that went out with no bearer (cookie-only)", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    const client = new ApiClientBase("https://api.test");

    const res = await client.requestWithCredential("/api/x");

    expect(res.sentToken).toBeNull();
    expect(res.sentSub).toBeNull();
    expect(sentAuthorization(spy)).toBeUndefined();
  });

  it("captures the token stored AT SEND TIME, not an earlier one (another tab wrote B)", async () => {
    const client = new ApiClientBase("https://api.test");
    client.setToken(tokenFor("A"));
    localStorage.setItem("auth_token", tokenFor("B")); // another tab, no event
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));

    const res = await client.requestWithCredential("/api/x");

    expect(res.sentSub).toBe("B");
  });

  it("reports sentSub null for a non-JWT bearer but still the token sent", async () => {
    localStorage.setItem("auth_token", "opaque");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    const res = await new ApiClientBase(
      "https://api.test",
    ).requestWithCredential("/api/x");
    expect(res.sentToken).toBe("opaque");
    expect(res.sentSub).toBeNull();
  });

  it("request() is unchanged: it still returns only the data", async () => {
    localStorage.setItem("auth_token", tokenFor("1"));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({ a: 2 }));
    const data = await new ApiClientBase("https://api.test").request("/api/x");
    expect(data).toEqual({ a: 2 });
  });

  it("the public auth client reports the bearer it sent, null when none", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    const client = new PublicAuthClient();
    const first = await client.requestWithCredential("/api/auth/profile");
    expect(first.sentToken).toBeNull();

    const token = tokenFor("9");
    client.setToken(token);
    const second = await client.requestWithCredential("/api/auth/profile");
    expect(second.sentToken).toBe(token);
    expect(second.sentSub).toBe("9");
    expect(spy).toHaveBeenCalledTimes(2);
  });
});
