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

import {
  COOKIE as TAX_IV_COOKIE,
  mintToken as mintTaxInterviewToken,
} from "@/app/(assessment)/tax_interview/_server/access";
import {
  ASSESSMENT_COOKIE,
  mintAssessmentToken,
  resetAssessmentRateLimitForTests,
} from "../_access";
import { GET as sessionGet } from "../session/route";
import { POST as submitPost } from "./route";

const STUB_RELAY = ["rt05", "relay", "stub"].join("-");
const ROUTE_SRC = readFileSync(resolve(__dirname, "route.ts"), "utf-8");
const ALLOWED_RECIPIENT =
  /payload\.to !== "([^"]+)"/.exec(ROUTE_SRC)?.[1] ?? "";
const SAME_ORIGIN = "http://localhost";

function makeSubmitReq(
  body: unknown,
  opts: {
    cookie?: string | null;
    ip?: string;
    origin?: string | null;
    headers?: Record<string, string>;
  } = {},
): NextRequest {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "x-forwarded-for": opts.ip ?? "10.10.0.1",
    ...(opts.headers ?? {}),
  };
  const origin = opts.origin === undefined ? SAME_ORIGIN : opts.origin;
  if (origin !== null) {
    headers.Origin = origin;
  }
  if (opts.cookie) {
    headers.Cookie = opts.cookie;
  }
  return new NextRequest("http://localhost/api/assessment/submit", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function validPayload(overrides: Record<string, unknown> = {}) {
  return {
    to: ALLOWED_RECIPIENT,
    subject: "[Assessment] Kandidat — Blok 1 (12m 3s)",
    body: "<p>My answer</p>",
    ...overrides,
  };
}

function sessionCookie(): string {
  return `${ASSESSMENT_COOKIE}=${mintAssessmentToken()!}`;
}

describe("RT-05: /api/assessment/session and /api/assessment/submit", () => {
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

  it("GET /api/assessment/session sets a signed HttpOnly SameSite=Strict cookie, no-store", async () => {
    const res = await sessionGet(
      new NextRequest("http://localhost/api/assessment/session"),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toMatch(/^bz_assessment_session=\d+\.[A-Za-z0-9_-]+;/);
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=Strict");
    expect(setCookie).toContain("Path=/");
    expect(setCookie).toContain(`Max-Age=${12 * 60 * 60}`);
  });

  it("GET /api/assessment/session answers 503 and sets no cookie without the relay secret", async () => {
    vi.stubEnv("INTERNAL_API_KEY", "");
    vi.stubEnv("API_KEY", "");
    const res = await sessionGet(
      new NextRequest("http://localhost/api/assessment/session"),
    );
    expect(res.status).toBe(503);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("cache-control")).toBe("no-store, private");
  });

  it("the cookie from GET /api/assessment/session is accepted by submit", async () => {
    const sessionRes = await sessionGet(
      new NextRequest("http://localhost/api/assessment/session"),
    );
    const match = /bz_assessment_session=([^;]+)/.exec(
      sessionRes.headers.get("set-cookie") ?? "",
    );
    expect(match).not.toBeNull();

    const res = await submitPost(
      makeSubmitReq(validPayload(), {
        cookie: `${ASSESSMENT_COOKIE}=${match![1]}`,
      }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    expect((init?.headers as Record<string, string>)["X-API-Key"]).toBe(
      STUB_RELAY,
    );
  });

  it("accepts a valid tax_interview cookie in place of the assessment cookie", async () => {
    const taxToken = mintTaxInterviewToken(Date.now() + 60_000)!;
    const res = await submitPost(
      makeSubmitReq(validPayload(), {
        cookie: `${TAX_IV_COOKIE}=${taxToken}`,
      }),
    );
    expect(res.status).toBe(200);
  });

  it("returns 503 when INTERNAL_API_KEY / API_KEY is missing", async () => {
    const cookie = sessionCookie();
    vi.stubEnv("INTERNAL_API_KEY", "");
    vi.stubEnv("API_KEY", "");

    const res = await submitPost(makeSubmitReq(validPayload(), { cookie }));
    expect(res.status).toBe(503);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects a missing, cross-site or opaque Origin (403) before the session check", async () => {
    const cookie = sessionCookie();
    for (const origin of [
      null,
      "https://evil.example",
      "https://balizero.com.evil.example",
      "http://balizero.com",
      "null",
      "not a url",
    ]) {
      const res = await submitPost(
        makeSubmitReq(validPayload(), { cookie, origin }),
      );
      expect(res.status).toBe(403);
      expect(res.headers.get("cache-control")).toBe("no-store, private");
    }
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("accepts Origin balizero.com, www.balizero.com and the current origin", async () => {
    const cookie = sessionCookie();
    for (const origin of [
      "https://balizero.com",
      "https://www.balizero.com",
      SAME_ORIGIN,
    ]) {
      const res = await submitPost(
        makeSubmitReq(validPayload(), { cookie, origin }),
      );
      expect(res.status).toBe(200);
    }
    expect(global.fetch).toHaveBeenCalledTimes(3);
  });

  it("rejects a same-origin request without a session cookie (401)", async () => {
    const res = await submitPost(makeSubmitReq(validPayload()));
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
        makeSubmitReq(validPayload(), {
          cookie: `${ASSESSMENT_COOKIE}=${bad}`,
        }),
      );
      expect(res.status).toBe(401);
    }
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects a non-allowlisted recipient, a subject without the [Assessment] prefix and CRLF injection (400)", async () => {
    const cookie = sessionCookie();
    const cases = [
      validPayload({ to: "untrusted-recipient" }),
      validPayload({ subject: "Assessment Block 1" }),
      validPayload({ subject: " [Assessment] Block 1" }),
      validPayload({ subject: "[Assessment] Hello\r\nBcc: untrusted-recipient" }),
      validPayload({ subject: 42 }),
      validPayload({ body: "   " }),
    ];
    for (const payload of cases) {
      const res = await submitPost(makeSubmitReq(payload, { cookie }));
      expect(res.status).toBe(400);
    }
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("enforces subject <= 200 and body <= 65536 characters, plus the raw payload bound", async () => {
    const cookie = sessionCookie();
    const prefix = "[Assessment] ";
    const subjectAt = prefix + "S".repeat(200 - prefix.length);
    const bodyAt = "B".repeat(65_536);

    const atLimit = await submitPost(
      makeSubmitReq(validPayload({ subject: subjectAt, body: bodyAt }), {
        cookie,
      }),
    );
    expect(atLimit.status).toBe(200);

    const bigSubject = await submitPost(
      makeSubmitReq(validPayload({ subject: `${subjectAt}S` }), { cookie }),
    );
    expect(bigSubject.status).toBe(413);

    const bigBody = await submitPost(
      makeSubmitReq(validPayload({ body: `${bodyAt}B` }), { cookie }),
    );
    expect(bigBody.status).toBe(413);

    const declaredBig = await submitPost(
      makeSubmitReq(validPayload(), {
        cookie,
        headers: { "Content-Length": String(512 * 1024 + 1) },
      }),
    );
    expect(declaredBig.status).toBe(413);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("rate-limits excessive submissions per IP (429)", async () => {
    const cookie = sessionCookie();

    for (let i = 0; i < 10; i++) {
      const res = await submitPost(
        makeSubmitReq(validPayload({ subject: `[Assessment] Block ${i}` }), {
          cookie,
          ip: "10.10.0.99",
        }),
      );
      expect(res.status).toBe(200);
    }

    const blocked = await submitPost(
      makeSubmitReq(validPayload({ subject: "[Assessment] Block 11" }), {
        cookie,
        ip: "10.10.0.99",
      }),
    );
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("retry-after")).toBe("60");
    expect(global.fetch).toHaveBeenCalledTimes(10);
  });

  it("refused requests (403/401) do not consume the per-IP quota", async () => {
    for (let i = 0; i < 12; i++) {
      await submitPost(makeSubmitReq(validPayload(), { ip: "10.10.0.77" }));
    }
    const res = await submitPost(
      makeSubmitReq(validPayload(), {
        cookie: sessionCookie(),
        ip: "10.10.0.77",
      }),
    );
    expect(res.status).toBe(200);
  });
});
