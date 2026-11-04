import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { NextRequest } from "next/server";
import { POST as approvePost } from "@/app/api/intel/staging/approve/[type]/[id]/route";
import { POST as speechPost } from "@/app/api/audio/speech/route";

describe("Internal API BFF routes (intel approve & audio)", () => {
  const mockFetch = vi.fn();
  const originalEnv = process.env;

  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = mockFetch;
    process.env = {
      ...originalEnv,
      NUZANTARA_API_URL: "https://nuzantara-rag.fly.dev",
      NUZANTARA_INTERNAL_API_KEY: "internal-secret-key",
    };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("rejects unauthenticated requests with 401 before calling backend", async () => {
    const req = new Request(
      "http://localhost/api/intel/staging/approve/visa/item-1",
      { method: "POST" },
    ) as unknown as NextRequest;

    const res = await approvePost(req, {
      params: Promise.resolve({ type: "visa", id: "item-1" }),
    });

    expect(res.status).toBe(401);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("rejects portal client role with 403 and never forwards X-API-Key", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: { email: "uxxxx5@gmail.com", role: "client" },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const req = new Request(
      "http://localhost/api/intel/staging/approve/visa/item-1",
      {
        method: "POST",
        headers: { Authorization: "Bearer portal-jwt" },
      },
    ) as unknown as NextRequest;

    const res = await approvePost(req, {
      params: Promise.resolve({ type: "visa", id: "item-1" }),
    });

    expect(res.status).toBe(403);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("rejects non-editorial staff role (reception) on intel staging approve with 403", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: { email: "reception@example.com", role: "Reception" },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const req = new Request(
      "http://localhost/api/intel/staging/approve/visa/item-1",
      {
        method: "POST",
        headers: { Authorization: "Bearer reception-jwt" },
      },
    ) as unknown as NextRequest;

    const res = await approvePost(req, {
      params: Promise.resolve({ type: "visa", id: "item-1" }),
    });

    expect(res.status).toBe(403);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("forwards X-API-Key for verified staff member on intel staging approve", async () => {
    mockFetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: { email: "admin@example.com", role: "admin" },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            message: "Approved",
            id: "item-1",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );

    const req = new Request(
      "http://localhost/api/intel/staging/approve/visa/item-1",
      {
        method: "POST",
        headers: {
          Cookie: "nz_access_token=staff-jwt",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({}),
      },
    ) as unknown as NextRequest;

    const res = await approvePost(req, {
      params: Promise.resolve({ type: "visa", id: "item-1" }),
    });

    expect(res.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    const [targetUrl, init] = mockFetch.mock.calls[1] as [string, RequestInit];
    expect(targetUrl).toBe(
      "https://nuzantara-rag.fly.dev/api/intel/staging/approve/visa/item-1",
    );
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe(
      "internal-secret-key",
    );
  });

  it("forwards X-API-Key for verified staff member on audio/speech", async () => {
    mockFetch
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: {
              email: "consultant@example.com",
              role: "Junior Consultant",
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: { "content-type": "audio/mpeg" },
        }),
      );

    const req = new Request("http://localhost/api/audio/speech", {
      method: "POST",
      headers: {
        Authorization: "Bearer staff-jwt",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text: "Halo Bali Zero", voice: "alloy" }),
    }) as unknown as NextRequest;

    const res = await speechPost(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("audio/mpeg");
    const [targetUrl, init] = mockFetch.mock.calls[1] as [string, RequestInit];
    expect(targetUrl).toBe("https://nuzantara-rag.fly.dev/api/audio/speech");
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe(
      "internal-secret-key",
    );
  });
});
