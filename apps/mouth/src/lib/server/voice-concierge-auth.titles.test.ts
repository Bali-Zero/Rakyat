import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { canAccessVoiceConciergeHeaders } from "./voice-concierge-auth";

// Reorganisation 2026-11-09: each new job title must be admitted exactly like
// the title it replaces, with an internal AND with an external email.
const TITLE_PAIRS: Array<[string, string]> = [
  ["Supervisor", "Direktur"],
  ["Tax Lead", "Tax Supervisor"],
  ["Team Leader", "Tax Responsible"],
  ["Executive Consultant", "Senior Consultant"],
  ["Accounting", "HR & Accounting"],
];

describe("voice concierge auth: 2026-11-09 job titles", () => {
  const originalFetch = global.fetch;
  const originalUrl = process.env.NUZANTARA_API_URL;

  beforeEach(() => {
    vi.stubEnv("NODE_ENV", "production");
    process.env.NUZANTARA_API_URL = "https://backend.test/api";
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    global.fetch = originalFetch;
    if (originalUrl === undefined) delete process.env.NUZANTARA_API_URL;
    else process.env.NUZANTARA_API_URL = originalUrl;
  });

  async function admits(role: string, email: string): Promise<boolean> {
    global.fetch = vi.fn().mockResolvedValue(Response.json({ email, role }));
    return canAccessVoiceConciergeHeaders(
      new Headers({ cookie: "nz_access_token=session-token" }),
    );
  }

  it.each(TITLE_PAIRS)(
    "external email: %s and its replacement %s get the same answer",
    async (oldTitle, newTitle) => {
      const external = "someone@example.org";
      expect(await admits(newTitle, external)).toBe(
        await admits(oldTitle, external),
      );
    },
  );

  it.each(TITLE_PAIRS)(
    "internal email: %s and its replacement %s are both admitted",
    async (oldTitle, newTitle) => {
      const internal = "someone@balizero.com";
      expect(await admits(oldTitle, internal)).toBe(true);
      expect(await admits(newTitle, internal)).toBe(true);
    },
  );

  it("accounting titles are admitted by role alone", async () => {
    expect(await admits("Accounting", "someone@example.org")).toBe(true);
    expect(await admits("HR & Accounting", "someone@example.org")).toBe(true);
  });

  it("guilt control: an unknown title with an external email is refused", async () => {
    expect(await admits("Visitor Liaison", "someone@example.org")).toBe(false);
  });
});
