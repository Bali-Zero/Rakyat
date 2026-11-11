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
  const init = spy.mock.calls[spy.mock.calls.length - 1][1] as {
    headers: Record<string, string>;
  };
  return init.headers["Authorization"];
}

describe("requestWithCredential", () => {
  beforeEach(() => {
    // Order-independent: no spy, call history or stored token from a
    // previous test may leak in (run with --sequence.shuffle to check).
    vi.restoreAllMocks();
    vi.clearAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

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

describe("sentToken comes from the FINAL headers passed to fetch", () => {
  beforeEach(() => {
    // Order-independent: no spy, call history or stored token from a
    // previous test may leak in (run with --sequence.shuffle to check).
    vi.restoreAllMocks();
    vi.clearAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  async function sendWith(headers?: Record<string, string>, stored?: string) {
    if (stored) localStorage.setItem("auth_token", stored);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    return new ApiClientBase("https://api.test").requestWithCredential(
      "/api/x",
      headers ? { headers } : {},
    );
  }

  it("managed token", async () => {
    const t = tokenFor("1");
    const r = await sendWith(undefined, t);
    expect([r.sentToken, r.sentSub]).toEqual([t, "1"]);
  });

  it("caller-supplied Bearer with no managed token is what is sent, so it is reported", async () => {
    const t = tokenFor("2");
    const r = await sendWith({ Authorization: `Bearer ${t}` });
    expect([r.sentToken, r.sentSub]).toEqual([t, "2"]);
  });

  it("caller-supplied lower-case header with no managed token is reported", async () => {
    const t = tokenFor("2");
    const r = await sendWith({ authorization: `Bearer ${t}` });
    expect([r.sentToken, r.sentSub]).toEqual([t, "2"]);
  });

  it("caller-supplied non-Bearer or malformed Authorization is null/null", async () => {
    for (const value of [
      "Basic abc",
      "Bearer",
      "Bearer a b",
      "Bearer a, Bearer b",
      "",
    ]) {
      const r = await sendWith({ Authorization: value });
      expect([r.sentToken, r.sentSub]).toEqual([null, null]);
    }
  });

  it("any value a reader might not take as a bearer is null/null (no trim, exact scheme)", async () => {
    const t = tokenFor("1");
    for (const value of [
      ` Bearer ${t}`,
      `Bearer ${t} `,
      ` Bearer ${t}`,
      `Bearer ${t} `,
      `\tBearer ${t}`,
      `bearer ${t}`,
      `BEARER ${t}`,
      `Bearer  ${t}`,
      `Bearer\t${t}`,
      `Bearer ${t}`,
    ]) {
      const r = await sendWith({ Authorization: value });
      expect([r.sentToken, r.sentSub]).toEqual([null, null]);
    }
  });

  it("the public auth client: a padded caller header with a stored token is null/null", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    const r = await new PublicAuthClient().requestWithCredential(
      "/api/auth/portal-superuser",
      { headers: { Authorization: ` Bearer ${tokenFor("A")}` } },
    );
    expect([r.sentToken, r.sentSub]).toEqual([null, null]);
  });

  it("two Authorization spellings (fetch would join them) is null/null", async () => {
    const r = await sendWith({ authorization: "Bearer zzz" }, tokenFor("1"));
    expect([r.sentToken, r.sentSub]).toEqual([null, null]);
  });

  it("no Authorization at all is null/null", async () => {
    const r = await sendWith({ "X-Other": "1" });
    expect([r.sentToken, r.sentSub]).toEqual([null, null]);
  });

  it("the public auth client reports a caller-supplied Bearer and null for non-Bearer", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}));
    const client = new PublicAuthClient();
    const t = tokenFor("5");
    const a = await client.requestWithCredential("/api/auth/profile", {
      headers: { Authorization: `Bearer ${t}` },
    });
    expect([a.sentToken, a.sentSub]).toEqual([t, "5"]);
    const b = await client.requestWithCredential("/api/auth/profile", {
      headers: { Authorization: "Basic abc" },
    });
    expect([b.sentToken, b.sentSub]).toEqual([null, null]);
  });
});
