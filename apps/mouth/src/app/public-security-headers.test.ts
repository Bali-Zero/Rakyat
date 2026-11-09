import { describe, expect, it } from "vitest";

import nextConfig, {
  PUBLIC_CSP_REPORT_ONLY,
  sentryCspReportUri,
} from "../../next.config";

type Header = { key: string; value: string };

async function publicHeaders(): Promise<Header[]> {
  const rules = await nextConfig.headers!();
  const rule = rules.find((r) => r.source === "/:path*");
  expect(rule, 'a header rule for source "/:path*"').toBeDefined();
  return rule!.headers as Header[];
}

const directive = (name: string) =>
  PUBLIC_CSP_REPORT_ONLY.split("; ").find((d) => d.startsWith(`${name} `));

/**
 * Measured on balizero.com 2026-11-01: `/`, `/kbli`, the service pages and
 * the articles answered with NO Content-Security-Policy and NO
 * Cross-Origin-Opener-Policy; only /portal carried a policy.
 */
describe("public routes carry COOP and a report-only CSP", () => {
  it("sets Cross-Origin-Opener-Policy for every path", async () => {
    const coop = (await publicHeaders()).find(
      (h) => h.key === "Cross-Origin-Opener-Policy",
    );
    expect(coop?.value).toBe("same-origin-allow-popups");
  });

  it("sets Content-Security-Policy-Report-Only, never the enforcing header", async () => {
    const headers = await publicHeaders();
    expect(
      headers.find((h) => h.key === "Content-Security-Policy-Report-Only")
        ?.value,
    ).toBe(PUBLIC_CSP_REPORT_ONLY);
    expect(
      headers.find((h) => h.key === "Content-Security-Policy"),
    ).toBeUndefined();
  });

  it("closes the directives that need no inventory", () => {
    expect(directive("frame-ancestors")).toBe("frame-ancestors 'none'");
    expect(directive("object-src")).toBe("object-src 'none'");
    expect(directive("base-uri")).toBe("base-uri 'self'");
    expect(directive("form-action")).toBe("form-action 'self'");
  });

  it("stays compatible with static rendering: no nonce, no eval", () => {
    expect(PUBLIC_CSP_REPORT_ONLY).not.toMatch(/'nonce-/);
    expect(PUBLIC_CSP_REPORT_ONLY).not.toMatch(/'unsafe-eval'/);
  });

  it("lets the browser talk to the RAG backend and GA4", () => {
    const connect = directive("connect-src")!;
    expect(connect).toContain("https://nuzantara-rag.fly.dev");
    expect(connect).toContain("wss://nuzantara-rag.fly.dev");
    expect(connect).toContain("https://www.google-analytics.com");
    expect(directive("script-src")).toContain(
      "https://www.googletagmanager.com",
    );
  });
});

describe("sentryCspReportUri", () => {
  it("derives the security endpoint from a public DSN", () => {
    expect(
      sentryCspReportUri("https://abc123@o4509.ingest.us.sentry.io/4510"),
    ).toBe(
      "https://o4509.ingest.us.sentry.io/api/4510/security/?sentry_key=abc123",
    );
  });

  it("returns null without a DSN or with a malformed one", () => {
    expect(sentryCspReportUri(undefined)).toBeNull();
    expect(sentryCspReportUri("")).toBeNull();
    expect(sentryCspReportUri("not a url")).toBeNull();
    expect(sentryCspReportUri("https://o1.ingest.sentry.io/")).toBeNull();
  });
});
