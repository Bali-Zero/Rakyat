import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

/**
 * Acceptance guard: Sentry Session Replay is DISABLED on mouth.
 *
 * Replay records every navigation URL (the command palette opens `/clients?q=<typed text>`) in
 * the replay event and in the recording's performance spans, and `beforeSend` never runs for
 * replay, so the structured URL scrub cannot protect it. Re-enabling Replay needs its own
 * scrubbing design, a new spec and a new gate: until then this test goes red if any replay
 * integration or replay sample rate comes back in a mouth Sentry config.
 */
const MOUTH = join(__dirname, "..", "..");
const FORBIDDEN =
  /replayIntegration|replayCanvasIntegration|replaysSessionSampleRate|replaysOnErrorSampleRate/;

function configFiles(): string[] {
  const roots = readdirSync(MOUTH).filter(
    (name) =>
      /^sentry\..*\.ts$/.test(name) ||
      /^instrumentation(-client)?\.ts$/.test(name),
  );
  const src = join(MOUTH, "src");
  const inSrc = ["instrumentation.ts", "instrumentation-client.ts"].filter(
    (name) => existsSync(join(src, name)),
  );
  return [
    ...roots.map((name) => join(MOUTH, name)),
    ...inSrc.map((name) => join(src, name)),
  ];
}

describe("Sentry Replay is disabled on mouth", () => {
  it("finds the client, server and edge configs (the guard looks at something)", () => {
    const names = configFiles().map((f) => f.split("/").pop());
    expect(names).toEqual(
      expect.arrayContaining([
        "sentry.client.config.ts",
        "sentry.server.config.ts",
        "sentry.edge.config.ts",
      ]),
    );
  });

  it.each(configFiles())(
    "%s has no replay integration or sample rate",
    (file) => {
      // comments may say "replay"; only the API names are forbidden
      const code = readFileSync(file, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/^\s*\/\/.*$/gm, "");
      expect(code).not.toMatch(FORBIDDEN);
    },
  );

  it("the client init receives no replay integration and no replay sample rate", async () => {
    const init = vi.fn();
    vi.resetModules();
    vi.doMock("@sentry/nextjs", () => ({ init }));
    await import("../../sentry.client.config");
    const options = init.mock.calls[0][0] as Record<string, unknown>;
    expect(options).not.toHaveProperty("replaysSessionSampleRate");
    expect(options).not.toHaveProperty("replaysOnErrorSampleRate");
    const names = ((options.integrations as unknown[]) ?? []).map((i) =>
      String((i as { name?: string })?.name ?? i),
    );
    expect(names.join(",")).not.toMatch(/replay/i);
    vi.doUnmock("@sentry/nextjs");
  });
});
