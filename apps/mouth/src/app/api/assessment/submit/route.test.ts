import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: {
    error: vi.fn(),
    warn: vi.fn(),
    info: vi.fn(),
    debug: vi.fn(),
  },
}));

vi.mock("../_access", async (orig) => {
  const real = await orig<typeof import("../_access")>();
  return { ...real, sleep: vi.fn(async () => {}) };
});

import {
  ASSESSMENT_COOKIE,
  mintAssessmentToken,
  resetAssessmentRateLimitForTests,
} from "../_access";
import { DELETE as unlockDelete, POST as unlockPost } from "../unlock/route";
import { POST as submitPost } from "./route";

const STUB_RELAY = ["rt05", "relay", "stub"].join("-");
const ROUTE_SRC = readFileSync(resolve(__dirname, "route.ts"), "utf-8");
const ALLOWED_RECIPIENT =
  /payload\.to !== "([^"]+)"/.exec(ROUTE_SRC)?.[1] ?? "";

function makeSubmitReq(
  body: unknown,
  opts: {
    cookie?: string | null;
    ip?: string;
    headers?: Record<string, string>;
  } = {},
): NextRequest {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "x-forwarded-for": opts.ip ?? "10.10.0.1",
    ...(opts.headers ?? {}),
  };
  if (opts.cookie) {
    headers.Cookie = opts.cookie;
  }
  return new NextRequest("http://localhost/api/assessment/submit", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("RT-05: /api/assessment/unlock and /api/assessment/submit", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    resetAssessmentRateLimitForTests();
    vi.stubEnv("INTERNAL_API_KEY", STUB_RELAY);
    vi.stubEnv("NUZANTARA_API_URL", "https://backend.invalid");
    global.fetch = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ success: true, message: "sent" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
  });

  it("returns 503 when INTERNAL_API_KEY / API_KEY is missing", async () => {
    const token = mintAssessmentToken()!;
    vi.stubEnv("INTERNAL_API_KEY", "");
    vi.stubEnv("API_KEY", "");

    const res = await submitPost(
      makeSubmitReq(
        { to: ALLOWED_RECIPIENT, subject: "Test", body: "Answer" },
        { cookie: `${ASSESSMENT_COOKIE}=${token}` },
      ),
    );
    expect(res.status).toBe(503);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects anonymous requests without cookie (401)", async () => {
    const res = await submitPost(
      makeSubmitReq({
        to: ALLOWED_RECIPIENT,
        subject: "Test",
        body: "Answer",
      }),
    );
    expect(res.status).toBe(401);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects tampered or expired assessment cookies (401)", async () => {
    const valid = mintAssessmentToken(Date.now() + 60_000)!;
    const tampered = `${valid}x`;
    const expired = mintAssessmentToken(Date.now() - 1000)!;

    for (const bad of [tampered, expired, "not-a-token", "123.sig"]) {
      const res = await submitPost(
        makeSubmitReq(
          { to: ALLOWED_RECIPIENT, subject: "Test", body: "Answer" },
          { cookie: `${ASSESSMENT_COOKIE}=${bad}` },
        ),
      );
      expect(res.status).toBe(401);
    }
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("issues a valid session cookie via POST /api/assessment/unlock and accepts submit", async () => {
    const badUnlock = await unlockPost(
      new NextRequest("http://localhost/api/assessment/unlock", {
        method: "POST",
        headers: { "x-forwarded-for": "10.10.0.2" },
        body: JSON.stringify({ pin: "WRONG" }),
      }),
    );
    expect(badUnlock.status).toBe(401);

    const unlockRes = await unlockPost(
      new NextRequest("http://localhost/api/assessment/unlock", {
        method: "POST",
        headers: { "x-forwarded-for": "10.10.0.2" },
        body: JSON.stringify({ pin: "SUNSET-2026" }),
      }),
    );
    expect(unlockRes.status).toBe(200);
    const setCookie = unlockRes.headers.get("set-cookie") ?? "";
    const match = /bz_assessment_session=([^;]+)/.exec(setCookie);
    expect(match).not.toBeNull();

    const submitRes = await submitPost(
      makeSubmitReq(
        {
          to: ALLOWED_RECIPIENT,
          subject: "[Assessment] Block 1",
          body: "<p>My answer</p>",
        },
        { cookie: `${ASSESSMENT_COOKIE}=${match![1]}`, ip: "10.10.0.2" },
      ),
    );
    expect(submitRes.status).toBe(200);
    expect(submitRes.headers.get("cache-control")).toBe("no-store, private");
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    expect((init?.headers as Record<string, string>)["X-API-Key"]).toBe(
      STUB_RELAY,
    );

    const delRes = await unlockDelete(
      new NextRequest("http://localhost/api/assessment/unlock", {
        method: "DELETE",
      }),
    );
    expect(delRes.headers.get("set-cookie")).toMatch(
      /bz_assessment_session=;.*Max-Age=0/,
    );
  });

  it("rejects non-allowlisted recipient and CRLF subject injection (400)", async () => {
    const token = mintAssessmentToken()!;
    const cookie = `${ASSESSMENT_COOKIE}=${token}`;

    const badTo = await submitPost(
      makeSubmitReq(
        { to: "untrusted-recipient", subject: "Hi", body: "x" },
        { cookie },
      ),
    );
    expect(badTo.status).toBe(400);

    const crlfSubject = await submitPost(
      makeSubmitReq(
        {
          to: ALLOWED_RECIPIENT,
          subject: "Hello\r\nBcc: untrusted-recipient",
          body: "x",
        },
        { cookie },
      ),
    );
    expect(crlfSubject.status).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects oversized subject, body, and raw payload (413)", async () => {
    const token = mintAssessmentToken()!;
    const cookie = `${ASSESSMENT_COOKIE}=${token}`;

    const bigSubject = await submitPost(
      makeSubmitReq(
        { to: ALLOWED_RECIPIENT, subject: "S".repeat(300), body: "ok" },
        { cookie },
      ),
    );
    expect(bigSubject.status).toBe(413);

    const bigBody = await submitPost(
      makeSubmitReq(
        { to: ALLOWED_RECIPIENT, subject: "ok", body: "B".repeat(100_005) },
        { cookie },
      ),
    );
    expect(bigBody.status).toBe(413);

    const declaredBig = await submitPost(
      makeSubmitReq(
        { to: ALLOWED_RECIPIENT, subject: "ok", body: "ok" },
        { cookie, headers: { "Content-Length": String(128 * 1024 + 1) } },
      ),
    );
    expect(declaredBig.status).toBe(413);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rate-limits excessive submissions per IP (429)", async () => {
    const token = mintAssessmentToken()!;
    const cookie = `${ASSESSMENT_COOKIE}=${token}`;

    for (let i = 0; i < 10; i++) {
      const res = await submitPost(
        makeSubmitReq(
          { to: ALLOWED_RECIPIENT, subject: `Block ${i}`, body: "ans" },
          { cookie, ip: "10.10.0.99" },
        ),
      );
      expect(res.status).toBe(200);
    }

    const blocked = await submitPost(
      makeSubmitReq(
        { to: ALLOWED_RECIPIENT, subject: "Block 11", body: "ans" },
        { cookie, ip: "10.10.0.99" },
      ),
    );
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("retry-after")).toBe("60");
    expect(global.fetch).toHaveBeenCalledTimes(10);
  });
});
