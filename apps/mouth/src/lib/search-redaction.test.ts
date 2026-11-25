import { describe, it, expect } from "vitest";
import { pathOnly, safeUpstreamError } from "./search-redaction";

const VALUE = "zetaquorum-fixture-search";

describe("pathOnly", () => {
  it("drops the query and the fragment", () => {
    expect(pathOnly(`https://kita.example/clients?q=${VALUE}#top`)).toBe(
      "/clients",
    );
    expect(pathOnly("/process?search=x")).toBe("/process");
  });

  it.each([
    `https://kita.example/clients]?q=${VALUE}`,
    `https://kita.example/clients)?q=${VALUE}`,
    `https://kita.example/clients:?q=${VALUE}`,
    `https://kita.example/clients?q=prefix;${VALUE}`,
    `https://kita.example/clients?q=a%26b%3D${VALUE}`,
    `https://kita.example/clients#q=${VALUE}`,
  ])("never keeps the value of %s", (raw) => {
    expect(pathOnly(raw)).not.toContain(VALUE);
  });

  it("returns null for nothing", () => {
    expect(pathOnly(null)).toBeNull();
    expect(pathOnly("")).toBeNull();
  });
});

describe("safeUpstreamError and credential paths", () => {
  it("replaces the secret segment of a credential route in message and stack, keeping the message", () => {
    const secret = "tkfixture42zz"; // pragma: allowlist secret
    const raw = new TypeError(
      `Failed to parse URL from http://backend/api/funnel_email/unsubscribe/${secret}; GET /api/visa/match/${secret} failed`,
    );
    const safe = safeUpstreamError(
      raw,
      `http://backend/api/funnel_email/unsubscribe/${secret}`,
      `http://backend/api/funnel_email/unsubscribe/${secret}`,
      "",
    );
    expect(safe.message).not.toContain(secret);
    expect(String(safe.stack)).not.toContain(secret);
    expect(safe.message).toContain("Failed to parse URL from");
    expect(safe.message).toContain("/api/funnel_email/unsubscribe/[REDACTED]");
    expect(safe.message).toContain("/api/visa/match/[REDACTED]");
  });
});

describe("safeUpstreamError and a path logged as a placeholder", () => {
  const secret = "tkfixture42zz"; // pragma: allowlist secret
  const path = `/api/visa/m%61tch/${secret}`;

  it("replaces the raw request path wherever the message carries it", () => {
    const raw = new TypeError(
      `Failed to parse URL from http://elsewhere.test${path}`,
    );
    const safe = safeUpstreamError(
      raw,
      `http://backend${path}`,
      "http://backend[REDACTED]",
      "",
      path,
      "[REDACTED]",
    );
    expect(safe.message).not.toContain(secret);
    expect(String(safe.stack)).not.toContain(secret);
    expect(safe.message).toBe(
      "Failed to parse URL from http://elsewhere.test[REDACTED]",
    );
  });

  it("leaves the message alone when no request path is given", () => {
    const raw = new TypeError(
      "Failed to parse URL from http://elsewhere.test/api/x/y",
    );
    const safe = safeUpstreamError(
      raw,
      "http://backend/api/x/y",
      "http://backend/api/x/y",
      "",
      "",
      "[REDACTED]",
    );
    expect(safe.message).toBe(raw.message);
  });
});

describe("safeUpstreamError", () => {
  const target = `http://backend/api/crm/clients?search=${VALUE}`;
  const search = `?search=${VALUE}`;

  it("removes the exact URL and query from message and stack, and drops the cause", () => {
    const raw = Object.assign(
      new TypeError(`Failed to parse URL from ${target}`),
      { cause: new Error(`inner ${target}`) },
    );
    const safe = safeUpstreamError(
      raw,
      target,
      "http://backend/api/crm/clients",
      search,
    );
    expect(safe.name).toBe("TypeError");
    expect(safe.message).toBe(
      "Failed to parse URL from http://backend/api/crm/clients",
    );
    expect(String(safe.stack)).not.toContain(VALUE);
    expect((safe as { cause?: unknown }).cause).toBeUndefined();
  });

  it("leaves an unrelated error as it was (innocence)", () => {
    const raw = new Error("connect ECONNREFUSED 10.0.0.1:443");
    const safe = safeUpstreamError(raw, target, "http://backend/x", search);
    expect(safe.message).toBe("connect ECONNREFUSED 10.0.0.1:443");
    expect(safe.stack).toBe(raw.stack);
  });
});
