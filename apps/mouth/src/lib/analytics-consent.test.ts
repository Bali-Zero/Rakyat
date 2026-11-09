import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { consentModeScript } from "./analytics-consent";

type FakeWindow = {
  location: { hostname?: string; pathname?: string };
  dataLayer?: unknown[];
};

/** Run the inline script against a fake window; return the gtag calls it queued. */
function run(hostname: string | undefined, pathname = "/"): unknown[][] {
  const win: FakeWindow = { location: { hostname, pathname } };
  new Function("window", consentModeScript)(win);
  return (win.dataLayer ?? []).map((args) =>
    Array.from(args as ArrayLike<unknown>),
  );
}

const DEFAULT_DENIED = [
  "consent",
  "default",
  {
    analytics_storage: "denied",
    ad_storage: "denied",
    ad_user_data: "denied",
    ad_personalization: "denied",
    wait_for_update: 500,
  },
];
const GRANT = ["consent", "update", { analytics_storage: "granted" }];

describe("consentModeScript (COM-253)", () => {
  it.each([
    ["my.balizero.com", "/portal"],
    ["my.balizero.com", "/portal/documents"],
    ["my.balizero.com", "/"],
    ["MY.BALIZERO.COM.", "/portal/visa"],
    ["www.my.balizero.com", "/portal"],
    ["staging.my.balizero.com", "/"],
    ["balizero.com", "/PORTAL/Documents"],
    ["balizero.com", "/portal/login"],
    ["kita.balizero.com", "/portal/magic"],
    ["x.my.balizero.com", "/"],
    ["localhost", "/portal"],
    ["balizero.com", "/portal-guide"],
    ["www.balizero.com", "/portalx"],
  ])("portal %s%s: default denied, no automatic grant", (host, path) => {
    const calls = run(host, path);
    expect(calls).toEqual([DEFAULT_DENIED]);
  });

  it.each([
    ["", "/"],
    [undefined, "/"],
    ["localhost", "/"],
    ["mouth-preview.fly.dev", "/"],
    ["balizero.com.evil.example", "/"],
    ["notbalizero.com", "/"],
  ])("uncertain host %s: fail-closed, no automatic grant", (host, path) => {
    expect(run(host, path)).toEqual([DEFAULT_DENIED]);
  });

  it.each([
    ["balizero.com", "/"],
    ["www.balizero.com", "/visa"],
    ["kita.balizero.com", "/dashboard"],
    ["visa.balizero.com", "/"],
    ["nuzantara.co.id", "/"],
    ["balizero.com", "/port"],
  ])(
    "marketing host %s%s: unchanged — default denied then grant",
    (host, path) => {
      expect(run(host, path)).toEqual([DEFAULT_DENIED, GRANT]);
    },
  );

  it("fails closed when the host cannot be read", () => {
    const win = {
      location: {
        get hostname(): string {
          throw new Error("no host");
        },
        pathname: "/",
      },
      dataLayer: undefined as unknown[] | undefined,
    };
    new Function("window", consentModeScript)(win);
    const calls = (win.dataLayer ?? []).map((a) =>
      Array.from(a as ArrayLike<unknown>),
    );
    expect(calls).toEqual([DEFAULT_DENIED]);
  });

  it("never grants ad_* storage on any host", () => {
    expect(consentModeScript).not.toMatch(/ad_[a-z_]+'\s*:\s*'granted'/);
  });

  it("root layout ships this script and no hard-coded grant of its own", () => {
    const layout = readFileSync(
      join(__dirname, "..", "app", "layout.tsx"),
      "utf8",
    );
    expect(layout).not.toContain("gtag('consent','update'");
    expect(layout).toContain("__html: consentModeScript");
  });
});
