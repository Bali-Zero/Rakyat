/**
 * Contract tests for the Newsroom / Blog / Intel vertical (swarm API parity
 * audit, Vertical 4). Each case pins one front-to-back contract fix:
 *
 *  - DEFECT-3A-01  articles.api.ts never double-prefixes `/api/api/articles/*`
 *  - DEFECT-3A-04  newsletter.ts has no `"undefined/api/..."` URL when the env is unset
 *  - DEFECT-3C-01  subscribe() accepts the backend `SubscribeResponse { subscriberId }`
 *  - DEFECT-3D-03  newsletter errors read FastAPI `detail` (string or `{ message }`)
 *  - DEFECT-3D-05  failed email sends are counted as failed, never as sent
 *  - DEFECT-3B-01  POST /api/blog/newsletter/confirm is handled (no 405)
 *  - DEFECT-3A-05  confirm route resolves the deployed NUZANTARA_API_URL
 *
 * All fetch traffic is mocked; no network, no real addresses.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const loggerMock = vi.hoisted(() => ({
  error: vi.fn(),
  warn: vi.fn(),
  info: vi.fn(),
  debug: vi.fn(),
}));

vi.mock("@/lib/logger", () => ({ logger: loggerMock }));
vi.mock("../../logger", () => ({ logger: loggerMock }));

const clientSpy = vi.hoisted(() => ({
  baseUrls: [] as string[],
  request: vi.fn(),
}));

vi.mock("../client", () => ({
  ApiClientBase: class {
    constructor(baseUrl: string) {
      clientSpy.baseUrls.push(baseUrl);
    }
    request = clientSpy.request;
  },
}));

vi.mock("next/server", () => ({
  NextResponse: {
    json: (body: unknown, init?: { status?: number }) => ({
      kind: "json",
      body,
      status: init?.status ?? 200,
    }),
    redirect: (url: URL | string) => ({ kind: "redirect", url: String(url) }),
  },
  NextRequest: class {},
}));

const fetchMock = vi.mocked(global.fetch);

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 400) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  } as unknown as Response;
}

function subscribeRequest(overrides: Record<string, unknown> = {}) {
  return {
    email: "placeholder-subscriber",
    categories: ["business"],
    frequency: "weekly",
    language: "en",
    ...overrides,
  } as never;
}

describe("DEFECT-3A-01 articles.api base URL", () => {
  beforeEach(() => {
    vi.resetModules();
    clientSpy.baseUrls.length = 0;
    clientSpy.request.mockReset();
    clientSpy.request.mockResolvedValue({ success: true });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it.each(["/api", "/api/", "", "https://api.example.invalid/api"])(
    "never produces a doubled /api/api prefix when NEXT_PUBLIC_API_URL=%j",
    async (envValue) => {
      vi.stubEnv("NEXT_PUBLIC_API_URL", envValue);
      const { articlesApi } = await import("../articles.api");

      await articlesApi.getStatus();
      await articlesApi.compose({
        title: "A valid article title",
        content: "x".repeat(120),
        category: "immigration",
        author: "Marketing Team",
      });

      const base = clientSpy.baseUrls[0];
      const endpoints = clientSpy.request.mock.calls.map((c) => c[0] as string);
      expect(endpoints).toEqual([
        "/api/articles/compose/status",
        "/api/articles/compose",
      ]);
      for (const endpoint of endpoints) {
        expect(`${base}${endpoint}`).not.toContain("/api/api/");
        expect(`${base}${endpoint}`.endsWith(endpoint)).toBe(true);
      }
    },
  );
});

describe("newsletter.ts contract fixes", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    loggerMock.error.mockReset();
    loggerMock.warn.mockReset();
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function load(publicBase = "") {
    vi.stubEnv("NEXT_PUBLIC_ZANTARA_API_URL", publicBase);
    vi.stubEnv("ZANTARA_API_URL", "");
    return import("../../blog/newsletter");
  }

  it("DEFECT-3A-04: builds same-origin URLs, never 'undefined/api/...'", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ success: true, message: "ok", subscriberId: "s-1" }),
    );
    const { NewsletterService } = await load();

    await NewsletterService.subscribe(subscribeRequest());

    expect(fetchMock.mock.calls[0][0]).toBe("/api/blog/newsletter/subscribe");
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("undefined");
  });

  it("DEFECT-3C-01: accepts SubscribeResponse { subscriberId } and does not re-send the confirmation message", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        success: true,
        message: "Please check your email to confirm your subscription.",
        subscriberId: "sub-42",
      }),
    );
    const { NewsletterService } = await load("https://zantara.test");

    await expect(
      NewsletterService.subscribe(subscribeRequest({ frequency: "monthly" })),
    ).resolves.toEqual({
      success: true,
      subscriberId: "sub-42",
      message: "Please check your email to confirm your subscription.",
    });
    // Exactly one call: the backend already sends the double-opt-in message.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("DEFECT-3D-03: surfaces FastAPI detail (object and string) instead of 'Subscription failed'", async () => {
    const { NewsletterService, extractNewsletterError } = await load(
      "https://zantara.test",
    );

    expect(
      extractNewsletterError(
        {
          detail: {
            message: "Already subscribed",
            code: "ALREADY_SUBSCRIBED",
          },
        },
        "fallback",
      ),
    ).toBe("Already subscribed");
    expect(extractNewsletterError({ detail: "Invalid" }, "fallback")).toBe(
      "Invalid",
    );
    expect(extractNewsletterError({ message: "legacy" }, "fallback")).toBe(
      "legacy",
    );
    expect(extractNewsletterError(null, "fallback")).toBe("fallback");

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: { message: "Already subscribed" } }, false, 409),
    );
    await expect(
      NewsletterService.subscribe(subscribeRequest({ categories: [] })),
    ).resolves.toEqual({
      success: false,
      message: "Already subscribed",
    });
  });

  it("DEFECT-3D-05: a rejected send counts as failed, not sent (weekly digest)", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          articles: [
            {
              id: "a1",
              slug: "a",
              title: "T",
              excerpt: "e",
              coverImage: "/c.jpg",
              category: "business",
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          subscribers: [
            {
              id: "s1",
              email: "placeholder-subscriber",
              categories: ["business"],
              frequency: "weekly",
              language: "en",
              confirmed: true,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ detail: "down" }, false, 503));
    const { NewsletterService } = await load("https://zantara.test");

    await expect(NewsletterService.sendWeeklyDigest()).resolves.toEqual({
      sent: 0,
      articlesIncluded: 1,
    });
  });
});

describe("newsletter confirm BFF route", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("DEFECT-3B-01 + 3A-05: exports POST and forwards to NUZANTARA_API_URL", async () => {
    vi.stubEnv("NUZANTARA_API_URL", "https://backend.test");
    vi.stubEnv("ZANTARA_API_URL", "");
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ success: true, message: "Subscription confirmed" }),
    );
    const route = await import("../../../app/api/blog/newsletter/confirm/route");

    expect(typeof route.GET).toBe("function");
    expect(typeof route.POST).toBe("function");

    const request = {
      url: "https://balizero.com/api/blog/newsletter/confirm",
      json: async () => ({ subscriberId: "sub-1", token: "tok" }),
    };
    const res = (await route.POST(request as never)) as unknown as {
      status: number;
      body: { success: boolean };
    };

    expect(fetchMock).toHaveBeenCalledWith(
      "https://backend.test/api/blog/newsletter/confirm",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ subscriberId: "sub-1", token: "tok" }),
      }),
    );
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it("relays upstream 404 and rejects malformed bodies with 422", async () => {
    vi.stubEnv("NUZANTARA_API_URL", "https://backend.test");
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Invalid confirmation link" }, false, 404),
    );
    const route = await import("../../../app/api/blog/newsletter/confirm/route");

    const notFound = (await route.POST({
      url: "https://balizero.com/api/blog/newsletter/confirm",
      json: async () => ({ subscriberId: "sub-1", token: "bad" }),
    } as never)) as unknown as { status: number };
    expect(notFound.status).toBe(404);

    const malformed = (await route.POST({
      url: "https://balizero.com/api/blog/newsletter/confirm",
      json: async () => ({ token: "only-token" }),
    } as never)) as unknown as { status: number };
    expect(malformed.status).toBe(422);
  });

  it("DEFECT-3A-05: ignores relative NEXT_PUBLIC_API_URL='/api' so server fetch uses an origin", async () => {
    vi.stubEnv("NUZANTARA_API_URL", "");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "/api");
    vi.stubEnv("ZANTARA_API_URL", "");
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ success: true, message: "Confirmed" }),
    );
    const route = await import("../../../app/api/blog/newsletter/confirm/route");

    const res = (await route.POST({
      url: "https://balizero.com/api/blog/newsletter/confirm",
      json: async () => ({ subscriberId: "sub-2", token: "tok-2" }),
    } as never)) as unknown as { status: number };

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://nuzantara-rag.fly.dev/api/blog/newsletter/confirm",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("DEFECT-3C-05 AskZantara safe source rendering", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("renders source snippet without broken anchor when url is omitted or null", async () => {
    const React = await import("react");
    const { render, screen, fireEvent, waitFor } = await import(
      "@testing-library/react"
    );
    const { AskZantara } = await import(
      "../../../components/blog/interactive/AskZantara"
    );

    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        answer: "KITAS renewals start 30 days prior.",
        sources: [
          { title: "Immigration Guide", snippet: "Apply 30 days before expiry", url: null },
          { title: "Official Portal", snippet: "Portal link", url: "https://example.invalid/doc" },
        ],
        confidence: 0.9,
      }),
    );

    const { container } = render(
      React.createElement(AskZantara, { context: "KITAS Renewal" }),
    );
    const input = screen.getByLabelText("Ask Zantara a question");
    fireEvent.change(input, { target: { value: "When should I renew?" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      expect(
        screen.getByText("KITAS renewals start 30 days prior."),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("Immigration Guide")).toBeInTheDocument();
    expect(screen.getByText("Apply 30 days before expiry")).toBeInTheDocument();

    const anchors = Array.from(container.querySelectorAll("a"));
    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute("href")).toBe("https://example.invalid/doc");
  });
});

