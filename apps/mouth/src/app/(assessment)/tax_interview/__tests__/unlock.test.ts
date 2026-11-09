import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/app/(assessment)/tax_interview/_server/access", async (orig) => {
  const real = await orig<typeof import("../_server/access")>();
  return { ...real, sleep: vi.fn(async () => {}) };
});

import * as access from "../_server/access";
import { DELETE, POST } from "@/app/api/tax-interview/unlock/route";

const raw = (body: string, ip = "9.9.9.9") =>
  new NextRequest("http://localhost/api/tax-interview/unlock", {
    method: "POST",
    headers: { "x-forwarded-for": ip },
    body,
  });
const req = (pin: unknown, ip?: string) => raw(JSON.stringify({ pin }), ip);
/** True when the cookie value has a dot-separated field equal to the PIN. */
const cookiePayloadHasPin = (value: string): boolean =>
  value.split(".").some((part) => part === "666");

const cookieValue = (res: Response) =>
  /bz_tax_iv=([^;]*)/.exec(res.headers.get("set-cookie") ?? "")?.[1];

beforeEach(() => {
  vi.stubEnv("INTERNAL_API_KEY", "test-secret-not-real");
  access.resetThrottleForTests();
  vi.mocked(access.sleep).mockClear();
});
afterEach(() => vi.unstubAllEnvs());

describe("POST /api/tax-interview/unlock", () => {
  it("answers 401 to a wrong PIN and sets no cookie", async () => {
    const res = await POST(req("665"));
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(await res.json()).toEqual({ ok: false, error: "INVALID_PIN" });
  });

  it("treats an empty PIN, a number and malformed JSON as failures", async () => {
    for (const r of [req(""), req(666), raw("{not json"), raw("[]"), raw("")])
      expect((await POST(r)).status).toBe(401);
  });

  it("sets an HttpOnly, host-only, signed cookie for the right PIN", async () => {
    const res = await POST(req(" 666 "));
    expect(res.status).toBe(200);
    const header = res.headers.get("set-cookie")!;
    expect(header).toMatch(/^bz_tax_iv=\d+\.[\w-]+;/);
    expect(header).toContain("HttpOnly");
    expect(header).toContain("SameSite=Lax");
    expect(header).toContain("Max-Age=43200");
    expect(header).toContain("Path=/");
    expect(header).not.toMatch(/Domain=/i);
    // The cookie carries an expiry and a signature, never the PIN: assert on the
    // decoded payload, not on a substring that a random signature could contain.
    expect(cookiePayloadHasPin(cookieValue(res)!)).toBe(false);
    expect(access.verifyToken(cookieValue(res))).toBe(true);
    expect(res.headers.get("cache-control")).toContain("no-store");
  });

  it("guilt control: the PIN check does fire on a cookie that carries the PIN", () => {
    expect(cookiePayloadHasPin("1700000000000.666")).toBe(true);
    expect(cookiePayloadHasPin("666.abc")).toBe(true);
    expect(cookiePayloadHasPin("1700000000000.abc666def")).toBe(false);
  });

  it("rejects tampered, expired, foreign-key and malformed cookies", async () => {
    const good = cookieValue(await POST(req("666")))!;
    const [exp, sig] = good.split(".");
    expect(access.verifyToken(good)).toBe(true);
    expect(
      access.verifyToken(
        `${exp}.${sig.slice(0, -1)}${sig.endsWith("A") ? "B" : "A"}`,
      ),
    ).toBe(false);
    expect(access.verifyToken(`${Number(exp) + 1000}.${sig}`)).toBe(false);
    expect(access.verifyToken(good, Number(exp) + 1)).toBe(false);
    expect(access.verifyToken("1")).toBe(false);
    expect(access.verifyToken(".abc")).toBe(false);
    expect(access.verifyToken(undefined)).toBe(false);
    vi.stubEnv("INTERNAL_API_KEY", "another-secret");
    expect(access.verifyToken(good)).toBe(false);
  });

  it("fails closed without the server secret: 503, no cookie, nothing verifies", async () => {
    const good = cookieValue(await POST(req("666")))!;
    vi.stubEnv("INTERNAL_API_KEY", "");
    vi.stubEnv("API_KEY", "");
    const res = await POST(req("666"));
    expect(res.status).toBe(503);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(access.verifyToken(good)).toBe(false);
  });

  it("waits a fixed 400 ms on a wrong and on a right PIN", async () => {
    await POST(req("1"));
    await POST(req("666"));
    expect(access.GUESS_DELAY_MS).toBe(400);
    expect(vi.mocked(access.sleep).mock.calls).toEqual([[400], [400]]);
  });

  it("locks an IP after 10 wrong guesses (even the right PIN gets 429), others unaffected, success resets", async () => {
    for (let i = 0; i < 10; i++)
      expect((await POST(req("000", "1.1.1.1"))).status).toBe(401);
    expect((await POST(req("666", "1.1.1.1"))).status).toBe(429);
    expect((await POST(req("666", "2.2.2.2"))).status).toBe(200);
    for (let i = 0; i < 9; i++) await POST(req("000", "3.3.3.3"));
    expect((await POST(req("666", "3.3.3.3"))).status).toBe(200);
    for (let i = 0; i < 9; i++) await POST(req("000", "3.3.3.3"));
    expect((await POST(req("000", "3.3.3.3"))).status).toBe(401);
  });

  it("DELETE clears the cookie", async () => {
    const res = await DELETE(
      new NextRequest("http://localhost/api/tax-interview/unlock", {
        method: "DELETE",
      }),
    );
    expect(res.headers.get("set-cookie")).toMatch(/bz_tax_iv=;.*Max-Age=0/);
  });
});
