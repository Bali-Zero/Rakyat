import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "./route";

const STUB_BEARER = ["rt06", "indexnow", "stub"].join("-");

function makeReq(body: unknown, auth?: string): NextRequest {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (auth !== undefined) {
    headers.Authorization = auth;
  }
  return new NextRequest("http://localhost/api/indexnow", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

describe("RT-06: POST /api/indexnow fail-closed auth & domain allowlist", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("INDEXNOW_SECRET", STUB_BEARER);
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 200 }));
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
  });

  it("fails closed with 503 when INDEXNOW_SECRET is unset or empty", async () => {
    vi.stubEnv("INDEXNOW_SECRET", "");
    const res = await POST(makeReq({ urls: ["/business/a"] }));
    expect(res.status).toBe(503);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects missing or invalid Bearer authorization with 401", async () => {
    expect((await POST(makeReq({ urls: ["/a"] }))).status).toBe(401);
    expect(
      (await POST(makeReq({ urls: ["/a"] }, "Bearer wrong-value"))).status,
    ).toBe(401);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it.each([
    "https://evil.com/a",
    "https://balizero.com.evil.com/a",
    "//evil.com/a",
    "http://balizero.com/a",
    "https://balizero.com:8443/a",
    "javascript:alert(1)",
    (() => {
      const u = new URL("https://evil.com/a");
      u.username = "balizero.com";
      return u.href;
    })(),
  ])("rejects off-domain or malformed URL %s with 400", async (badUrl) => {
    const res = await POST(
      makeReq({ urls: [badUrl] }, `Bearer ${STUB_BEARER}`),
    );
    expect(res.status).toBe(400);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("accepts relative paths and https balizero.com / www.balizero.com URLs", async () => {
    const res = await POST(
      makeReq(
        {
          urls: [
            "/business/my-article",
            "https://balizero.com/visa",
            "https://www.balizero.com/tax",
          ],
        },
        `Bearer ${STUB_BEARER}`,
      ),
    );
    expect(res.status).toBe(200);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    const sent = JSON.parse(String(init?.body)) as {
      host: string;
      urlList: string[];
    };
    expect(sent.host).toBe("balizero.com");
    expect(sent.urlList).toEqual([
      "https://balizero.com/business/my-article",
      "https://balizero.com/visa",
      "https://www.balizero.com/tax",
    ]);
  });
});
