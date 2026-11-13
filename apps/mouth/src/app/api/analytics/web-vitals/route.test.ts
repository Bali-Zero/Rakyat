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

import { POST } from "./route";

const VALUE = "zetaquorum-fixture-search";

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

  it("from the referer fallback, encoded key included", async () => {
    await POST(
      beacon(
        { name: "LCP", value: 12 },
        `https://kita.example/process?se%61rch=${VALUE}&q=${VALUE}`,
      ),
    );
    expect(printed.join("\n")).not.toContain(VALUE);
    expect(printed.join("\n")).toContain("/process");
  });
});
