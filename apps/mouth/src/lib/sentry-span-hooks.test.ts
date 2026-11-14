import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { scrubSentryUrls } from "./sentry-url-scrub";

const VALUE = "zetaquorum-fixture-search";
const MOUTH = join(__dirname, "..", "..");

const CONFIGS = [
  "sentry.client.config.ts",
  "sentry.server.config.ts",
  "sentry.edge.config.ts",
] as const;

describe("mouth Sentry span / transaction scrubbing", () => {
  it.each(CONFIGS)("%s registers beforeSendSpan (not only dataCollection.urlQueryParams)", (file) => {
    const code = readFileSync(join(MOUTH, file), "utf8");
    expect(code).toMatch(/beforeSendSpan\s*:/);
    expect(code).toMatch(/scrubSentryUrls/);
  });

  it.each(CONFIGS)(
    "%s passes a non-identity beforeSendSpan into Sentry.init",
    async (file) => {
      const init = vi.fn();
      vi.resetModules();
      vi.doMock("@sentry/nextjs", () => ({ init, replayIntegration: vi.fn(() => ({})) }));
      vi.stubEnv("NODE_ENV", "production");
      vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "https://uxx9@dxxxx3.io/123");
      vi.stubEnv("SENTRY_DSN", "https://uxx9@dxxxx3.io/456");
      await import(`../../${file}`);
      const lastCall = init.mock.calls[init.mock.calls.length - 1];
      const options = lastCall?.[0] as Record<string, unknown>;
      expect(typeof options?.beforeSendSpan).toBe("function");
      const hook = options.beforeSendSpan as (span: unknown) => unknown;
      const span = {
        trace_id: "a",
        span_id: "b",
        name: `GET https://kita.example/clients?q=${VALUE}`,
        start_timestamp: 1,
        status: "ok" as const,
        is_segment: true,
        attributes: { "sentry.op": "http.client" },
      };
      const out = hook(span);
      expect(out).not.toBe(span);
      expect(JSON.stringify(out)).not.toContain(VALUE);
      vi.doUnmock("@sentry/nextjs");
      vi.unstubAllEnvs();
    },
  );

  it("beforeSendSpan scrubbing fails open without the hook (proves the wiring matters)", () => {
    const span = {
      trace_id: "a",
      span_id: "b",
      name: `GET /clients?q=${VALUE}`,
      start_timestamp: 1,
      status: "ok" as const,
      is_segment: true,
      attributes: {
        "sentry.op": "http.client",
        "url.full": `https://kita.example/clients?q=${VALUE}`,
      },
    };
    const scrubbed = scrubSentryUrls(structuredClone(span));
    expect(JSON.stringify(scrubbed)).not.toContain(VALUE);
    expect(JSON.stringify(span)).toContain(VALUE);
  });
});