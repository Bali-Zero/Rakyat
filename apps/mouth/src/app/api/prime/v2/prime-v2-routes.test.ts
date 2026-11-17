import { describe, it, expect, vi, beforeEach } from "vitest";
import { NextRequest } from "next/server";
import { PrimeApi, type AnalyzeRequest } from "@/lib/api/prime/prime.api";
import type { IApiClient } from "@/lib/api/types/api-client.types";

vi.mock("@/lib/logger", () => ({
  logger: {
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

import { POST as analyzePost } from "./analyze/route";
import { GET as densityGet } from "./density/route";
import { GET as intelligenceGet } from "./intelligence/route";
import { GET as portfolioGet } from "./portfolio/route";
import { GET as predictGet } from "./predict/route";
import { GET as proposalTokenGet } from "./proposal/[token]/route";
import { POST as proposalPost } from "./proposal/route";
import { GET as regulationsGet } from "./regulations/route";
import { GET as temporalGet } from "./temporal/route";

function makeGetRequest(url: string): NextRequest {
  return {
    nextUrl: new URL(url),
    headers: new Headers(),
  } as unknown as NextRequest;
}

describe("Prime v2 BFF routes", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = mockFetch;
  });

  it.each([
    ["2020 reused code", { kbli_code: "56102", vintage: "2020" }],
    ["2020 renumbered code", { kbli_code: "80100", vintage: "2020" }],
    ["2025 exact code", { kbli_code: "56102", vintage: "2025" }],
    ["omitted vintage", { kbli_code: "56102" }],
    ["zone only", {}],
    ["explicit empty code", { kbli_code: "" }],
  ] satisfies [string, Partial<AnalyzeRequest>][])(
    "forwards the browser analyze payload for %s",
    async (_name, fields) => {
      const input = { lat: -8.65, lng: 115.13, ...fields };
      const request = vi.fn();
      const prime = new PrimeApi({ request } as unknown as IApiClient);
      await prime.analyze(input);
      const [[endpoint, init]] = request.mock.calls;
      expect(endpoint).toBe("/api/prime/v2/analyze");
      mockFetch.mockResolvedValueOnce(Response.json({ status: "analyzed" }));
      const response = await analyzePost(
        new NextRequest(`http://browser${endpoint}`, init),
      );
      expect(response.status).toBe(200);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({
        ...input,
        is_pma: true,
      });
    },
  );

  it("propagates upstream 503 on analyze, density, predict, regulations, temporal, and proposal", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    });

    const analyzeReq = {
      json: async () => ({ lat: -8.65, lng: 115.13 }),
    } as unknown as NextRequest;
    expect((await analyzePost(analyzeReq)).status).toBe(503);

    const proposalReq = {
      json: async () => ({ zone_code: "C-1" }),
      headers: new Headers(),
    } as unknown as NextRequest;
    expect((await proposalPost(proposalReq)).status).toBe(503);

    expect(
      (
        await densityGet(
          makeGetRequest("http://localhost/api/prime/v2/density?zone_code=C-1"),
        )
      ).status,
    ).toBe(503);

    expect(
      (
        await predictGet(
          makeGetRequest("http://localhost/api/prime/v2/predict?zone_code=C-1"),
        )
      ).status,
    ).toBe(503);

    expect(
      (
        await regulationsGet(
          makeGetRequest(
            "http://localhost/api/prime/v2/regulations?zone_code=C-1",
          ),
        )
      ).status,
    ).toBe(503);

    expect(
      (
        await temporalGet(
          makeGetRequest(
            "http://localhost/api/prime/v2/temporal?zone_code=C-1",
          ),
        )
      ).status,
    ).toBe(503);
  });

  it("propagates upstream 401/403 on intelligence and portfolio, and 404 on proposal/[token]", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Forbidden" }),
    });
    expect(
      (
        await intelligenceGet(
          makeGetRequest(
            "http://localhost/api/prime/v2/intelligence?sw_lat=-8.7&sw_lng=115.1&ne_lat=-8.6&ne_lng=115.2",
          ),
        )
      ).status,
    ).toBe(403);

    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Unauthorized" }),
    });
    expect(
      (
        await portfolioGet(
          makeGetRequest(
            "http://localhost/api/prime/v2/portfolio?client_id=42",
          ),
        )
      ).status,
    ).toBe(401);

    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Not found" }),
    });
    expect(
      (
        await proposalTokenGet(
          makeGetRequest("http://localhost/api/prime/v2/proposal/tok123"),
          { params: Promise.resolve({ token: "tok123" }) },
        )
      ).status,
    ).toBe(404);
  });

  it("returns 502 when upstream fetch throws a network error", async () => {
    mockFetch.mockRejectedValue(new Error("connect ECONNREFUSED"));

    expect(
      (
        await predictGet(
          makeGetRequest("http://localhost/api/prime/v2/predict?zone_code=C-1"),
        )
      ).status,
    ).toBe(502);

    const proposalReq = {
      json: async () => ({ zone_code: "C-1" }),
      headers: new Headers(),
    } as unknown as NextRequest;
    expect((await proposalPost(proposalReq)).status).toBe(502);
  });

  it("sets Cache-Control: no-store, private on intelligence and portfolio responses", async () => {
    mockFetch.mockResolvedValueOnce(
      Response.json({ type: "FeatureCollection", features: [], stats: {} }),
    );
    const intelOk = await intelligenceGet(
      makeGetRequest(
        "http://localhost/api/prime/v2/intelligence?sw_lat=-8.7&sw_lng=115.1&ne_lat=-8.6&ne_lng=115.2",
      ),
    );
    expect(intelOk.status).toBe(200);
    expect(intelOk.headers.get("cache-control")).toBe("no-store, private");

    mockFetch.mockResolvedValueOnce(
      Response.json({ detail: "Unauthorized" }, { status: 401 }),
    );
    const intel401 = await intelligenceGet(
      makeGetRequest(
        "http://localhost/api/prime/v2/intelligence?sw_lat=-8.7&sw_lng=115.1&ne_lat=-8.6&ne_lng=115.2",
      ),
    );
    expect(intel401.status).toBe(401);
    expect(intel401.headers.get("cache-control")).toBe("no-store, private");

    mockFetch.mockResolvedValueOnce(
      Response.json({ client_id: 42, entities: [] }),
    );
    const portOk = await portfolioGet(
      makeGetRequest("http://localhost/api/prime/v2/portfolio?client_id=42"),
    );
    expect(portOk.status).toBe(200);
    expect(portOk.headers.get("cache-control")).toBe("no-store, private");

    mockFetch.mockResolvedValueOnce(
      Response.json({ detail: "Forbidden" }, { status: 403 }),
    );
    const port403 = await portfolioGet(
      makeGetRequest("http://localhost/api/prime/v2/portfolio?client_id=42"),
    );
    expect(port403.status).toBe(403);
    expect(port403.headers.get("cache-control")).toBe("no-store, private");
  });

  it("URL-encodes proposal token path segments and rejects dot traversal", async () => {
    const dotRes = await proposalTokenGet(
      makeGetRequest("http://localhost/api/prime/v2/proposal/.."),
      { params: Promise.resolve({ token: ".." }) },
    );
    expect(dotRes.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();

    mockFetch.mockResolvedValueOnce(Response.json({ id: "prop-1" }));
    const specialRes = await proposalTokenGet(
      makeGetRequest("http://localhost/api/prime/v2/proposal/encoded"),
      { params: Promise.resolve({ token: "tok/1?x=1#frag" }) },
    );
    expect(specialRes.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/prime/v2/proposal/tok%2F1%3Fx%3D1%23frag"),
    );
  });
});
