import { describe, it, expect, vi, beforeEach } from "vitest";

const printed: string[] = [];
vi.mock("@/lib/utils/console", () => ({
  debug: (...a: unknown[]) => printed.push(a.join(" ")),
  warn: (...a: unknown[]) => printed.push(a.join(" ")),
  error: (...a: unknown[]) => printed.push(a.join(" ")),
}));
vi.mock("@sentry/nextjs", () => ({
  captureException: vi.fn(),
  captureMessage: vi.fn(),
  setContext: vi.fn(),
  setUser: vi.fn(),
}));

import { logger } from "@/lib/logger";
import { POST } from "./route";

const VALUE = "zetaquorum-fixture-search";
const TAIL = "zetaquorum-fixture-tail";

function beacon(body: object, referer?: string): Request {
  return new Request("http://localhost/api/analytics/web-vitals", {
    method: "POST",
    headers: referer ? { referer } : {},
    body: JSON.stringify(body),
  });
}

describe("web-vitals ingest never logs the typed search text", () => {
  beforeEach(() => {
    printed.length = 0;
    vi.spyOn(console, "info").mockImplementation((...a: unknown[]) => {
      printed.push(a.join(" "));
    });
  });

  it("from the body url", async () => {
    const res = await POST(
      beacon({
        name: "LCP",
        value: 12,
        rating: "good",
        url: `https://kita.example/clients?search=${VALUE}`,
      }),
    );
    expect(res.status).toBe(204);
    expect(printed.join("\n")).not.toContain(VALUE);
    expect(printed.join("\n")).toContain("/clients");
  });

  it.each([
    `se%61rch=${VALUE}&q=${VALUE}`,
    `q=prefix;${TAIL}`,
    `q=prefix%3B${TAIL}`,
    `q=a%26b%3D${TAIL}`,
    `x=1#q=${VALUE}`,
  ])("from the referer fallback with ?%s", async (query) => {
    await POST(
      beacon(
        { name: "LCP", value: 12 },
        `https://kita.example/process?${query}`,
      ),
    );
    const out = printed.join("\n");
    expect(out).not.toContain(VALUE);
    expect(out).not.toContain(TAIL);
    expect(out).toContain("/process");
  });

  it("and the logger itself cuts a URL printed in a message or its metadata", () => {
    logger.info(`opened https://kita.example/clients?q=prefix;${TAIL}`, {
      metadata: { next: `/process?search=${VALUE}#q=${VALUE}` },
    });
    const out = printed.join("\n");
    expect(out).not.toContain(VALUE);
    expect(out).not.toContain(TAIL);
    expect(out).toContain("https://kita.example/clients");
    expect(out).toContain("/process");
  });
});
