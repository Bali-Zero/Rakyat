import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";

import {
  REDACTED,
  SENSITIVE_QUERY_PARAMS,
  analyticsSafeHref,
  analyticsSafePath,
  analyticsUrlRedactScript,
  buildAnalyticsUrlRedactScript,
  redactSensitiveQueryParams,
} from "./analytics-url";
import { TOKEN_PATH_PREFIXES, TOKEN_PATH_RULES } from "./sentry-url-scrub";
import {
  TELEMETRY_ADDRESS_CASES,
  TELEMETRY_CAMPAIGN_CASES,
  TELEMETRY_PATH_CASES,
  TELEMETRY_PATH_REDACTION,
} from "./telemetry-address-cases.fixture";

describe("redactSensitiveQueryParams", () => {
  it("redacts the invite token measured leaking to GA (portal audit L-GA)", () => {
    const out = redactSensitiveQueryParams(
      "https://my.balizero.com/portal/register?token=AUDITPROBE8400001000",
    );
    expect(out).not.toContain("AUDITPROBE8400001000");
    expect(out).toBe(
      `https://my.balizero.com/portal/register?token=${encodeURIComponent(REDACTED)}`,
    );
  });

  it("redacts the magic-link token", () => {
    const out = redactSensitiveQueryParams(
      "https://my.balizero.com/portal/magic?token=AUDITPROBE0840005000",
    );
    expect(out).not.toContain("AUDITPROBE0840005000");
  });

  it("redacts a token nested inside another parameter (the second leak)", () => {
    // The exact shape of the expired-magic-link redirect that leaked the token
    // a second time as dl=/dr=.
    const out = redactSensitiveQueryParams(
      "https://my.balizero.com/portal/login-upgraded?expired=true&reason=token_expired" +
        "&redirect=%2Fportal%2Fmagic%3Ftoken%3DAUDITPROBE0840005000",
    );
    expect(out).not.toContain("AUDITPROBE0840005000");
    expect(out).toContain("expired=true");
    expect(out).toContain("reason=token_expired");
  });

  it("redacts every declared parameter", () => {
    for (const param of SENSITIVE_QUERY_PARAMS) {
      const out = redactSensitiveQueryParams(
        `https://my.balizero.com/x?${param}=SUPERSECRETVALUE`,
      );
      expect(out, param).not.toContain("SUPERSECRETVALUE");
    }
  });

  it("leaves an ordinary URL byte-identical", () => {
    const clean = "https://balizero.com/kbli/56303?ref=newsletter&page=2";
    expect(redactSensitiveQueryParams(clean)).toBe(clean);
  });

  it("returns an unparseable value unchanged instead of throwing", () => {
    expect(redactSensitiveQueryParams("not a url")).toBe("not a url");
    expect(redactSensitiveQueryParams("")).toBe("");
  });
});

describe("analyticsSafeHref / analyticsSafePath (GA4 page_location, page_path)", () => {
  it.each(TELEMETRY_ADDRESS_CASES)(
    "cleans GA4 address row: $input",
    ({ input, ga4 }) => {
      expect(analyticsSafeHref(input)).toBe(ga4);
    },
  );
  const SECRET = "ga4fixturehash77"; // pragma: allowlist secret

  it("keeps origin and path only: no other query parameter, no fragment", () => {
    const out = analyticsSafeHref(
      "https://balizero.com/clients?search=zetaquorum&return_nonce=NONCE1&ref=x#frag",
    );
    expect(out).toBe("https://balizero.com/clients");
  });

  it("keeps ONLY the six campaign parameters, values unchanged, in original order", () => {
    const out = analyticsSafeHref(
      "https://balizero.com/kbli/56303?q=zetaquorum&utm_source=news%20letter&gclid=G123&return_nonce=NONCE1" +
        "&utm_medium=email&utm_campaign=c1&email=a%40b.example&utm_term=t&utm_content=ad&utm_x=no#frag",
    );
    expect(out).toBe(
      "https://balizero.com/kbli/56303?utm_source=news%20letter&gclid=G123&utm_medium=email&utm_campaign=c1&utm_term=t&utm_content=ad",
    );
  });

  it("masks a credential path while keeping the utm parameters; a credential path inside a utm value is redacted", () => {
    expect(
      analyticsSafeHref(
        `https://balizero.com/prime/proposal/${SECRET}?utm_source=a&return_nonce=N&utm_medium=b`,
      ),
    ).toBe(
      `https://balizero.com/prime/proposal/${REDACTED}?utm_source=a&utm_medium=b`,
    );
    const nested = analyticsSafeHref(
      `https://balizero.com/x?utm_content=%2Fvisa%2Fclock%2F${SECRET}&utm_source=a`,
    );
    expect(nested).not.toContain(SECRET);
    expect(nested).toContain("utm_source=a");
    const raw = analyticsSafeHref(
      `https://balizero.com/x?utm_content=/prime/proposal/${SECRET}`,
    );
    expect(raw).not.toContain(SECRET);
  });

  it.each(TOKEN_PATH_PREFIXES)(
    "replaces the secret segment after %s",
    (prefix) => {
      const out = analyticsSafeHref(
        `https://balizero.com${prefix}${SECRET}?a=1`,
      );
      expect(out).toBe(`https://balizero.com${prefix}${REDACTED}`);
      expect(analyticsSafePath(`${prefix}${SECRET}`)).toBe(
        `${prefix}${REDACTED}`,
      );
    },
  );

  it("replaces a root-relative value because GA4 admits absolute http(s) only", () => {
    expect(analyticsSafeHref("/visa/clock/" + SECRET + "?x=1")).toBe(REDACTED);
    expect(analyticsSafeHref("")).toBe("");
  });

  it("leaves an ordinary path alone (innocence)", () => {
    expect(analyticsSafeHref("https://balizero.com/kbli/56303")).toBe(
      "https://balizero.com/kbli/56303",
    );
    expect(analyticsSafePath("/kbli/56303")).toBe("/kbli/56303");
  });

  it.each(TELEMETRY_PATH_CASES)(
    "applies the shared path table: $path",
    ({ path, expected, rules }) => {
      const safeExpected = expected.replace(TELEMETRY_PATH_REDACTION, REDACTED);
      expect(analyticsSafeHref(`https://balizero.com${path}`, rules)).toBe(
        `https://balizero.com${safeExpected}`,
      );
      expect(analyticsSafePath(path, rules)).toBe(safeExpected);
    },
  );

  it.each(TELEMETRY_CAMPAIGN_CASES)(
    "applies the shared campaign table to %s",
    (query, expected) => {
      expect(analyticsSafeHref(`https://balizero.com/x?${query}`)).toBe(
        `https://balizero.com/x${expected ? `?${expected}` : ""}`,
      );
    },
  );
});

describe("analyticsUrlRedactScript", () => {
  const runBootstrap = (
    path: string,
    referrer = "",
    script = analyticsUrlRedactScript,
  ): unknown[] => {
    const win = window as typeof window & { dataLayer?: unknown[] };
    delete win.dataLayer;
    window.history.replaceState({}, "", path);
    Object.defineProperty(document, "referrer", {
      value: referrer,
      configurable: true,
    });
    new Function(script)();
    return win.dataLayer ?? [];
  };

  const effectiveLocation = (pushed: unknown[]): string =>
    pushed.length
      ? ((
          Array.from(pushed[pushed.length - 1] as IArguments)[1] as {
            page_location: string;
          }
        ).page_location ?? window.location.href)
      : window.location.href;

  it("is generated from the same parameter list (no drift between the two copies)", () => {
    for (const param of SENSITIVE_QUERY_PARAMS) {
      expect(analyticsUrlRedactScript).toContain(`"${param}"`);
    }
  });

  it("keeps the production bootstrap as the default rules build", () => {
    expect(buildAnalyticsUrlRedactScript(TOKEN_PATH_RULES)).toBe(
      analyticsUrlRedactScript,
    );
  });

  it("embeds the credential route prefixes of the scrubber (no drift)", () => {
    for (const prefix of TOKEN_PATH_PREFIXES) {
      expect(analyticsUrlRedactScript).toContain(JSON.stringify(prefix));
    }
  });

  it("overrides page_location only when the address carries a query, a fragment or a credential path", () => {
    // Real jsdom window/document: the script sets the GLOBAL dataLayer and
    // reads the browser's own location, exactly as it does in the browser.
    const win = window as typeof window & { dataLayer?: unknown[] };

    const run = (path: string, referrer: string): unknown[] => {
      delete win.dataLayer;
      window.history.replaceState({}, "", path);
      Object.defineProperty(document, "referrer", {
        value: referrer,
        configurable: true,
      });
      new Function(analyticsUrlRedactScript)();
      return win.dataLayer ?? [];
    };
    const payloadOf = (pushed: unknown[]) =>
      Array.from(pushed[0] as IArguments)[1] as {
        page_location: string;
        page_referrer: string;
      };

    expect(run("/kbli/56303", "")).toEqual([]);

    const pushed = run(
      "/portal/magic?token=LEAKME",
      "https://my.balizero.com/portal/register?token=LEAKMETOO",
    );
    expect(pushed).toHaveLength(1);
    expect(Array.from(pushed[0] as IArguments)[0]).toBe("set");
    const payload = payloadOf(pushed);
    expect(payload.page_location).not.toContain("LEAKME");
    expect(payload.page_referrer).not.toContain("LEAKMETOO");

    // typed search text and a nonce have no declared parameter name: still gone
    const search = payloadOf(
      run("/clients?search=zetaquorum&return_nonce=N0NCE", ""),
    );
    expect(search.page_location).not.toContain("zetaquorum");
    expect(search.page_location).not.toContain("N0NCE");
    expect(search.page_location).toContain("/clients");

    // the bootstrap keeps the campaign parameters too, the same six and nothing else
    const campaign = payloadOf(
      run(
        "/clients?q=zetaquorum&utm_source=a&return_nonce=N0NCE&gclid=G1&utm_x=no",
        "",
      ),
    );
    expect(campaign.page_location).toBe(
      "http://localhost:3000/clients?utm_source=a&gclid=G1",
    );
    const campaignToken = payloadOf(
      run("/visa/clock/CLOCKFIXTURE9?utm_source=a&q=zz", ""),
    );
    expect(campaignToken.page_location).toBe(
      "http://localhost:3000/visa/clock/[redacted]?utm_source=a",
    );

    // the path itself carries the credential
    const proposal = payloadOf(
      run("/visa/clock/CLOCKFIXTURE9", "https://x.example/prime/proposal/TKN9"),
    );
    expect(proposal.page_location).not.toContain("CLOCKFIXTURE9");
    expect(proposal.page_location).toContain("/visa/clock/[redacted]");
    expect(proposal.page_referrer).not.toContain("TKN9");
  });

  it.each(TELEMETRY_PATH_CASES)(
    "applies the shared bootstrap path table: $path",
    ({ path, expected, rules }) => {
      const pushed = runBootstrap(
        path,
        "",
        buildAnalyticsUrlRedactScript(rules),
      );
      expect(effectiveLocation(pushed)).toBe(
        new URL(
          expected.replace(TELEMETRY_PATH_REDACTION, REDACTED),
          "http://localhost:3000",
        ).href,
      );
    },
  );

  it.each(TELEMETRY_CAMPAIGN_CASES)(
    "applies the shared campaign rule in the bootstrap: %s",
    (query, expected) => {
      expect(effectiveLocation(runBootstrap(`/x?${query}`))).toBe(
        `http://localhost:3000/x${expected ? `?${expected}` : ""}`,
      );
    },
  );

  it("redacts the referrer campaign value while leaving a clean location alone", () => {
    const pushed = runBootstrap(
      "/kbli/56303",
      "https://example.test/x?utm_content=%2Fvisa%2Fclock%2Fmagic-link",
    );
    const payload = Array.from(pushed[pushed.length - 1] as IArguments)[1] as {
      page_location: string;
      page_referrer: string;
    };
    expect(payload.page_location).toBe("http://localhost:3000/kbli/56303");
    expect(payload.page_referrer).toBe(
      "https://example.test/x?utm_content=[redacted]",
    );
  });

  it.each([
    ["https://host.example?q=a/b", "https://host.example/"],
    [
      "https://host.example/api/visa/clock/{hash}",
      "https://host.example/api/visa/clock/{hash}",
    ],
    ["https://u:p@host.example/x", "https://host.example/x"],
    ["https:///u:p@host.example/x", REDACTED],
    ["https://host.example/visa/clock/zq-a/../zq-b", REDACTED],
    ["https://host.example/api/visa%2Fclock/zq-synthetic", REDACTED],
    ["https:\\\\host.example\\visa\\clock\\zq-synthetic", REDACTED],
    ["mailto:n@host.example", REDACTED],
    ["/clients?next=x", REDACTED],
    [
      "https://host.example/x%20y/%E2%9C%93",
      "https://host.example/x%20y/%E2%9C%93",
    ],
  ])("uses the raw referrer address rules: %s", (referrer, expected) => {
    const pushed = runBootstrap("/canonical?not_allowlisted=zq", referrer);
    const payload = Array.from(pushed[pushed.length - 1] as IArguments)[1] as {
      page_referrer: string;
    };
    expect(payload.page_referrer).toBe(expected);
  });

  it.each(TELEMETRY_ADDRESS_CASES)(
    "matches analyticsSafeHref for the shared GA4 fixture row: $input",
    ({ input }) => {
      const pushed = runBootstrap("/canonical?not_allowlisted=zq", input);
      const payload = Array.from(
        pushed[pushed.length - 1] as IArguments,
      )[1] as {
        page_referrer: string;
      };
      expect(payload.page_referrer).toBe(analyticsSafeHref(input));
    },
  );
});

describe("root layout wiring", () => {
  // The scrub only works if it is queued in dataLayer BEFORE gtag.js loads.
  // @next/third-parties' <GoogleAnalytics> injects the gtag('config', …)
  // whose automatic page_view is the beacon that leaked, so ordering is the
  // whole mechanism — not a style preference.
  const layout = readFileSync("src/app/layout.tsx", "utf8");

  it("mounts the redact script in <head>, before <GoogleAnalytics>", () => {
    const scrubAt = layout.indexOf("__html: analyticsUrlRedactScript");
    const headEndsAt = layout.indexOf("</head>");
    const gaAt = layout.indexOf("<GoogleAnalytics");
    expect(scrubAt).toBeGreaterThan(-1);
    expect(gaAt).toBeGreaterThan(-1);
    expect(scrubAt).toBeLessThan(headEndsAt);
    expect(scrubAt).toBeLessThan(gaAt);
  });
});
