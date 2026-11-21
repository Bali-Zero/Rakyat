import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

describe("RT-10: GET /api/portal/deadlines/ical private cache control", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("returns 401 with Cache-Control: no-store, private when unauthenticated", async () => {
    const req = new NextRequest("http://localhost/api/portal/deadlines/ical");
    const res = await GET(req);
    expect(res.status).toBe(401);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
  });

  it("sets Cache-Control: no-store, private on 200 iCal responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          upcoming_deadlines: [
            {
              id: "dl-1",
              label: "Visa renewal",
              due_date: "2026-12-01",
              kind: "visa",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const req = new NextRequest("http://localhost/api/portal/deadlines/ical", {
      headers: { Cookie: "nz_access_token=jwt-token-123" },
    });
    const res = await GET(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store, private");
    expect(res.headers.get("content-type")).toContain("text/calendar");
  });
});
