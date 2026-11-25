import { describe, it, expect } from "vitest";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import {
  redactTokenPaths,
  redactFreeText,
  encodeAsParser,
  pathOnlyUrl,
  scrubSentryUrls,
  TOKEN_PATH_PREFIXES,
  TOKEN_PATH_RULES,
} from "./sentry-url-scrub";
import {
  TELEMETRY_ADDRESS_CASES,
  TELEMETRY_FREE_TEXT_CASES,
  TELEMETRY_PATH_CASES,
  TELEMETRY_PATH_REDACTION,
} from "./telemetry-address-cases.fixture";

const VALUE = "zetaquorum-fixture-search";
const URLS = [
  `https://kita.example/clients?q=${VALUE}`,
  `https://kita.example/clients]?q=${VALUE}`,
  `https://kita.example/clients)?q=${VALUE}`,
  `https://kita.example/clients:?q=${VALUE}`,
  `https://kita.example/clients?q=prefix;${VALUE}`,
  `https://kita.example/clients?q=prefix%3B${VALUE}`,
  `https://kita.example/clients?q=a%26b%3D${VALUE}`,
  `https://kita.example/clients?se%61rch=${VALUE}`,
  `https://kita.example/clients#q=${VALUE}`,
  `/clients?search=${VALUE}`,
];

type RouteChildren = Record<string, string[]>;

function enumerateRouteChildren(
  appDir: string,
  routersDir: string,
  rules: readonly { prefix: string }[] = TOKEN_PATH_RULES,
): RouteChildren {
  const found: RouteChildren = Object.fromEntries(
    rules.map(({ prefix }) => [prefix, []]),
  );
  const add = (prefix: string, child: string) => {
    if (!found[prefix].includes(child)) found[prefix].push(child);
  };
  const transparent = (name: string) =>
    /^\(.*\)$/.test(name) || name.startsWith("@");
  const appWalk = (dir: string, segments: string[]) => {
    const prefix = `/${segments.join("/")}/`;
    if (found[prefix]) {
      const directChildren = (folder: string): string[] =>
        readdirSync(folder, { withFileTypes: true }).flatMap((entry) => {
          if (!entry.isDirectory()) return [];
          const full = join(folder, entry.name);
          return transparent(entry.name) ? directChildren(full) : [entry.name];
        });
      for (const child of directChildren(dir)) add(prefix, child);
    }
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      appWalk(
        join(dir, entry.name),
        transparent(entry.name) ? segments : [...segments, entry.name],
      );
    }
  };
  appWalk(appDir, []);

  for (const file of readdirSync(routersDir)) {
    if (!file.endsWith(".py")) continue;
    const source = readFileSync(join(routersDir, file), "utf8");
    const router = /APIRouter\([^)]*?prefix\s*=\s*"([^"]*)"/s.exec(source);
    const base = router ? router[1] : "";
    for (const route of source.matchAll(
      /@router\.(?:get|post|put|patch|delete)\(\s*"([^"]*)"/g,
    )) {
      const path = `${base}${route[1]}`;
      for (const { prefix } of rules) {
        if (!path.startsWith(prefix)) continue;
        const child = path.slice(prefix.length).split("/")[0];
        if (child) add(prefix, child);
      }
    }
  }
  return found;
}

function assertListedRouteChildren(
  children: RouteChildren,
  rules: readonly (typeof TOKEN_PATH_RULES)[number][] = TOKEN_PATH_RULES,
): void {
  for (const { prefix, staticChildren } of rules) {
    for (const child of children[prefix] ?? []) {
      const template =
        /^\[(?:\.\.\.)?[^\]]+\]$/.test(child) ||
        /^\{[^}]+\}$/.test(child) ||
        /^:[^/]+$/.test(child);
      if (!template && !staticChildren.includes(child)) {
        throw new Error(`${prefix}${child}`);
      }
    }
  }
}

describe("scrubSentryUrls", () => {
  it.each(URLS)("keeps only the path of %s", (url) => {
    const out = scrubSentryUrls({
      request: {
        url,
        query_string: `q=${VALUE}`,
        headers: {
          Referer: url,
          Link: url,
          Location: url,
          Accept: "text/html",
        },
      },
      breadcrumbs: [
        { category: "fetch", data: { url, "http.query": `q=${VALUE}` } },
      ],
      spans: [{ op: "http.client", description: `GET ${url}` }],
    });
    const blob = JSON.stringify(out);
    expect(blob).not.toContain(VALUE);
    expect(blob).toContain("/clients");
    expect(out.request.headers).toEqual({ Accept: "text/html" });
    expect(out.request.query_string).toBe("[REDACTED]");
    expect(out.spans[0].description.startsWith("GET ")).toBe(true);
  });

  it("filters header pairs and tolerates odd shapes", () => {
    const out = scrubSentryUrls({
      request: {
        headers: [
          ["Referer", "https://x/p?q=1"],
          ["Accept", "a"],
        ],
      },
      weird: null,
      n: 3,
    });
    expect(out.request.headers).toEqual([["Accept", "a"]]);
    expect(out.weird).toBeNull();
    expect(scrubSentryUrls("plain")).toBe("plain");
  });

  it.each([
    "Foo#bar",
    "Ready? retry now",
    "SELECT metadata#>>'{a}' FROM t",
    "issue #123 and q-learning",
  ])("never rewrites free text: %s", (text) => {
    const event = {
      message: text,
      exception: { values: [{ type: "Error", value: text }] },
    };
    expect(scrubSentryUrls(event)).toEqual(event);
  });

  it("leaves a URL without query or fragment untouched", () => {
    const event = { request: { url: "https://kita.example/clients/12" } };
    expect(scrubSentryUrls(event)).toEqual(event);
  });

  it("redacts dict values under keys search and q (parity with backend _scrub)", () => {
    const out = scrubSentryUrls({
      query_params: { search: VALUE, q: VALUE, " Q ": VALUE, month: "2026-11" },
    });
    expect(out).toEqual({
      query_params: {
        search: "[REDACTED]",
        q: "[REDACTED]",
        " Q ": "[REDACTED]",
        month: "2026-11",
      },
    });
  });

  it("scrubs streamed-span names and attributes for http spans", () => {
    const url = `https://kita.example/clients?q=${VALUE}`;
    const out = scrubSentryUrls({
      trace_id: "t",
      span_id: "s",
      name: `GET ${url}`,
      start_timestamp: 0,
      status: "ok",
      is_segment: true,
      attributes: {
        "sentry.op": "http.client",
        "url.full": url,
        "http.query": `q=${VALUE}`,
      },
    });
    const blob = JSON.stringify(out);
    expect(blob).not.toContain(VALUE);
    expect(out.name).toBe("GET https://kita.example/clients");
    expect(out.attributes["http.query"]).toBe("[REDACTED]");
  });

  it("drops the query string of http.target on a server span (guilt)", () => {
    const out = scrubSentryUrls({
      attributes: { "http.target": `/clients?q=${VALUE}` },
    });
    expect(out.attributes["http.target"]).toBe("/clients");
    expect(JSON.stringify(out)).not.toContain(VALUE);
  });

  it("leaves a clean http.target and non-URL attributes untouched (innocence)", () => {
    const event = {
      attributes: {
        "http.target": "/clients/12",
        "http.route": "/clients/[id]",
        "next.route": "/clients/[id]",
        "http.method": "GET",
      },
    };
    expect(scrubSentryUrls(event)).toEqual(event);
  });

  it("keeps only the path of both addresses of a navigation breadcrumb (guilt)", () => {
    const out = scrubSentryUrls({
      category: "navigation",
      data: {
        from: "/login?email=fixture%40example.com&pin=135790&redirect=%2Fclients",
        to: `https://kita.example/clients?q=${VALUE}`,
      },
    });
    expect(out).toEqual({
      category: "navigation",
      data: { from: "/login", to: "https://kita.example/clients" },
    });
    expect(JSON.stringify(out)).not.toContain("135790");
    expect(JSON.stringify(out)).not.toContain(VALUE);
  });

  it("leaves from/to of other breadcrumbs and clean navigations alone (innocence)", () => {
    const other = {
      category: "console",
      data: { from: "a?b=c", to: "d?e=f", arguments: ["x"] },
    };
    expect(scrubSentryUrls(other)).toEqual(other);
    const clean = {
      category: "navigation",
      data: { from: "/login", to: "/dashboard" },
    };
    expect(scrubSentryUrls(clean)).toEqual(clean);
  });

  describe("token path segments", () => {
    const SECRET = "tk3nfixture9zz"; // pragma: allowlist secret
    const cases = TOKEN_PATH_PREFIXES.flatMap((prefix) => [
      [`${prefix}${SECRET}`, `${prefix}[REDACTED]`],
      [`${prefix}${SECRET}/`, `${prefix}[REDACTED]/`],
      [`${prefix}${SECRET}?x=1`, `${prefix}[REDACTED]`],
      [
        `https://kita.example${prefix}${SECRET}`,
        `https://kita.example${prefix}[REDACTED]`,
      ],
      [
        `https://kita.example${prefix}${SECRET}/?x=1#h`,
        `https://kita.example${prefix}[REDACTED]/`,
      ],
    ]);

    it("lists the routes the finding and the route tree name", () => {
      expect(TOKEN_PATH_PREFIXES).toEqual(
        expect.arrayContaining([
          "/api/portal/invite/validate/",
          "/api/auth/verify-magic/",
        ]),
      );
    });

    it.each(cases)("replaces the token of %s (guilt)", (url, expected) => {
      const out = scrubSentryUrls({
        request: { url },
        breadcrumbs: [
          { category: "fetch", data: { url } },
          { category: "navigation", data: { from: url, to: url } },
        ],
        spans: [{ op: "http.client", description: `GET ${url}` }],
      });
      expect(JSON.stringify(out)).not.toContain(SECRET);
      expect(out.request.url).toBe(expected);
      expect(out.breadcrumbs[1].data.to).toBe(expected);
      expect(out.spans[0].description).toBe(`GET ${expected}`);
    });

    it("scrubs a streamed span name and a server span target", () => {
      const prefix = TOKEN_PATH_PREFIXES[0];
      const out = scrubSentryUrls({
        name: `GET ${prefix}${SECRET}`,
        attributes: { "sentry.op": "http.server" },
        data: { "http.target": `${prefix}${SECRET}` },
      });
      expect(JSON.stringify(out)).not.toContain(SECRET);
    });

    it("leaves the address of another page with a long segment alone (innocence)", () => {
      const event = {
        request: { url: `https://kita.example/clients/${SECRET}${SECRET}` },
        spans: [{ op: "http.client", description: `GET /api/other/${SECRET}` }],
      };
      expect(scrubSentryUrls(event)).toEqual(event);
    });

    it("keeps scrubbing query parameters on a token address (innocence)", () => {
      const prefix = TOKEN_PATH_PREFIXES[0];
      const out = scrubSentryUrls({
        request: { url: `${prefix}${SECRET}?q=${VALUE}` },
      });
      expect(JSON.stringify(out)).not.toContain(VALUE);
      expect(JSON.stringify(out)).not.toContain(SECRET);
    });

    it("scrubs url.path and every other path-bearing span attribute (Sentry 11.10)", () => {
      const prefix = TOKEN_PATH_PREFIXES[0];
      const path = `${prefix}${SECRET}`;
      const fields = [
        "url.path",
        "http.target",
        "http.url",
        "url.full",
        "browser.web_vital.lcp.url",
        "ui.element.url",
      ];
      for (const field of fields) {
        const out = scrubSentryUrls({
          op: "http.server",
          data: { [field]: `${path}?x=${VALUE}` },
          attributes: { [field]: { value: `${path}?x=1`, type: "string" } },
        });
        expect(JSON.stringify(out), field).not.toContain(SECRET);
        expect(JSON.stringify(out), field).not.toContain(VALUE);
      }
      expect(
        scrubSentryUrls({ data: { "url.path": path } }).data["url.path"],
      ).toBe(`${prefix}[REDACTED]`);
    });

    it("scrubs a transaction name and non-HTTP span names and descriptions", () => {
      const path = `${TOKEN_PATH_PREFIXES[5]}${SECRET}`;
      const out = scrubSentryUrls({
        transaction: `GET ${path}?q=${VALUE}`,
        spans: [
          { op: "navigation", description: `${path}?q=${VALUE}` },
          { op: "ui.react.render", description: `render page ${path}#h` },
        ],
        name: `navigation ${path}?q=${VALUE}`,
        attributes: { "sentry.segment.name": `${path}?q=${VALUE}` },
      });
      const text = JSON.stringify(out);
      expect(text).not.toContain(SECRET);
      expect(text).not.toContain(VALUE);
      expect(out.transaction).toBe(`GET ${TOKEN_PATH_PREFIXES[5]}[REDACTED]`);
    });

    it("replaces the structured query_string forms (array of pairs, object)", () => {
      const out = scrubSentryUrls({
        request: {
          query_string: [
            ["q", VALUE],
            ["return_nonce", SECRET],
          ],
        },
        contexts: { trace: { data: { "url.query": { q: VALUE } } } },
      });
      const text = JSON.stringify(out);
      expect(text).not.toContain(VALUE);
      expect(text).not.toContain(SECRET);
    });

    it("scrubs a credential path inside a message and an exception value, keeping the message", () => {
      for (const prefix of TOKEN_PATH_PREFIXES) {
        const out = scrubSentryUrls({
          message: `[Proxy] Fetch error for GET ${prefix}${SECRET}`,
          exception: {
            values: [
              {
                type: "TypeError",
                value: `Failed to parse URL from http://backend.test${prefix}${SECRET}/x`,
              },
            ],
          },
        });
        expect(JSON.stringify(out), prefix).not.toContain(SECRET);
        expect(out.message).toBe(
          `[Proxy] Fetch error for GET ${prefix}[REDACTED]`,
        );
        expect(out.exception.values[0].value).toBe(
          `Failed to parse URL from http://backend.test${prefix}[REDACTED]/x`,
        );
      }
    });

    it("leaves a message about another path alone (innocence)", () => {
      const event = {
        message: `Fetch error for GET /api/crm/practices/${SECRET}`,
        exception: { values: [{ type: "Error", value: "/visa/clock/ alone" }] },
      };
      expect(scrubSentryUrls(event)).toEqual(event);
      expect(redactTokenPaths("/visa/clock/")).toBe("/visa/clock/");
    });

    it.each(TELEMETRY_PATH_CASES)(
      "applies the shared path table: $path",
      ({ path, expected, rules }) => {
        expect(redactTokenPaths(path, "[REDACTED]", rules)).toBe(
          expected.replace(TELEMETRY_PATH_REDACTION, "[REDACTED]"),
        );
      },
    );

    it("keeps routing attributes while masking the same ordinary path", () => {
      const literal = "/api/visa/clock/zq-literal-route";
      const out = scrubSentryUrls({
        contexts: {
          trace: { data: { "http.route": literal, route: literal } },
        },
        spans: [
          {
            data: { "http.route": literal, route: literal },
            attributes: {
              "http.route": { value: literal, type: "string" },
              route: { value: literal, type: "string" },
              "url.path": { value: literal, type: "string" },
            },
          },
        ],
      });
      expect(out.contexts.trace.data).toEqual({
        "http.route": literal,
        route: literal,
      });
      expect(out.spans[0].data).toEqual({
        "http.route": literal,
        route: literal,
      });
      expect(out.spans[0].attributes["http.route"].value).toBe(literal);
      expect(out.spans[0].attributes.route.value).toBe(literal);
      expect(out.spans[0].attributes["url.path"].value).toBe(
        "/api/visa/clock/[REDACTED]",
      );
    });

    it("keeps a route transaction name and masks a URL transaction name", () => {
      const literal = "/api/visa/clock/zq-literal-route";
      expect(
        scrubSentryUrls({
          transaction: literal,
          transaction_info: { source: "route" },
        }).transaction,
      ).toBe(literal);
      expect(
        scrubSentryUrls({
          transaction: literal,
          transaction_info: { source: "url" },
        }).transaction,
      ).toBe("/api/visa/clock/[REDACTED]");
    });

    // ---- the route tree decides, not a hand list ---------------------------------------
    const CREDENTIAL_SEGMENT = /token|hash|nonce|secret/i;
    /** Reviewed: a directory whose name looks like a credential but is not one, with the reason. */
    const EXEMPT: Record<string, string> = {
      "/visa/voa/":
        "GARUDA result identifier, authenticated separately (round-1 inventory)",
      "/api/visa/voa/": "backend twin of /visa/voa/[hash], same identifier",
    };

    it("covers every credential-named dynamic folder of the app route tree", () => {
      const appDir = join(__dirname, "..", "app");
      const routers = join(
        __dirname,
        "..",
        "..",
        "..",
        "backend-rag",
        "backend",
        "app",
        "routers",
      );
      assertListedRouteChildren(enumerateRouteChildren(appDir, routers));
      const found: string[] = [];
      const walk = (dir: string) => {
        for (const entry of readdirSync(dir, { withFileTypes: true })) {
          if (!entry.isDirectory()) continue;
          const full = join(dir, entry.name);
          const dynamic = /^\[(?:\.\.\.)?([^\]]+)\]$/.exec(entry.name);
          if (dynamic && CREDENTIAL_SEGMENT.test(dynamic[1])) found.push(full);
          walk(full);
        }
      };
      walk(appDir);
      const prefixes = found.map((folder) => {
        const segments = relative(appDir, folder)
          .split(sep)
          .filter((s) => !/^\(.*\)$/.test(s) && !s.startsWith("@"));
        segments.pop();
        return `/${segments.join("/")}/`;
      });
      // the walk must really see the folders the finding names
      expect(prefixes).toEqual(
        expect.arrayContaining([
          "/prime/proposal/",
          "/visa/clock/",
          "/visa/match/",
        ]),
      );
      for (const prefix of prefixes) {
        if (EXEMPT[prefix]) continue;
        expect(TOKEN_PATH_PREFIXES, prefix).toContain(prefix);
        // and the masking acts on a real address of that route
        expect(
          scrubSentryUrls({ request: { url: `${prefix}${SECRET}` } }).request
            .url,
        ).toBe(`${prefix}[REDACTED]`);
      }
    });

    it("covers every credential path parameter of the backend routes the /api/[...path] proxy forwards", () => {
      const routers = join(
        __dirname,
        "..",
        "..",
        "..",
        "backend-rag",
        "backend",
        "app",
        "routers",
      );
      expect(existsSync(routers), routers).toBe(true);
      assertListedRouteChildren(
        enumerateRouteChildren(join(__dirname, "..", "app"), routers),
      );
      const found: string[] = [];
      for (const file of readdirSync(routers)) {
        if (!file.endsWith(".py")) continue;
        const src = readFileSync(join(routers, file), "utf8");
        const router = /APIRouter\([^)]*?prefix\s*=\s*"([^"]*)"/s.exec(src);
        const base = router ? router[1] : "";
        for (const route of src.matchAll(
          /@router\.(?:get|post|put|patch|delete)\(\s*"([^"]*)"/g,
        )) {
          const param = /\{([^}:]+)(?::[^}]*)?\}/.exec(route[1]);
          if (!param || !CREDENTIAL_SEGMENT.test(param[1])) continue;
          const before = route[1].slice(0, param.index);
          if (!before.endsWith("/")) continue;
          found.push(`${base}${before}`);
        }
      }
      expect(found).toEqual(
        expect.arrayContaining([
          "/api/funnel_email/unsubscribe/",
          "/api/visa/clock/",
          "/api/visa/match/",
          "/api/portal/invite/validate/",
          "/api/prime/v2/proposal/",
        ]),
      );
      for (const prefix of found) {
        if (EXEMPT[prefix]) continue;
        expect(TOKEN_PATH_PREFIXES, prefix).toContain(prefix);
      }
    });

    it("rejects an unlisted static child in a synthetic app route tree", () => {
      const root = mkdtempSync(join(tmpdir(), "zq-route-walk-"));
      try {
        const appDir = join(root, "app");
        const routers = join(root, "routers");
        mkdirSync(join(appDir, "api", "visa", "clock", "[hash]"), {
          recursive: true,
        });
        mkdirSync(join(appDir, "api", "visa", "clock", "zq-unlisted"), {
          recursive: true,
        });
        mkdirSync(routers);
        expect(() =>
          assertListedRouteChildren(enumerateRouteChildren(appDir, routers)),
        ).toThrow("/api/visa/clock/zq-unlisted");
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });

    it("rejects an unlisted static child in synthetic backend routes", () => {
      const root = mkdtempSync(join(tmpdir(), "zq-route-walk-"));
      try {
        const appDir = join(root, "app");
        const routers = join(root, "routers");
        mkdirSync(appDir);
        mkdirSync(routers);
        writeFileSync(
          join(routers, "zq_synthetic.py"),
          'router = APIRouter(prefix="/api/visa")\n@router.get("/clock/zq-unlisted-api")\n@router.get("/clock/{hash}")\n',
        );
        expect(() =>
          assertListedRouteChildren(enumerateRouteChildren(appDir, routers)),
        ).toThrow("/api/visa/clock/zq-unlisted-api");
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });
  });
});

describe("structured address admission", () => {
  it.each(TELEMETRY_ADDRESS_CASES)(
    "cleans structured Sentry address row: $input",
    ({ input, sentry }) => {
      expect(pathOnlyUrl(input)).toBe(sentry);
      const out = scrubSentryUrls({
        request: { url: input },
        breadcrumbs: [{ data: { url: input } }],
      });
      expect(out.request.url).toBe(sentry);
      expect(out.breadcrumbs[0].data.url).toBe(sentry);
    },
  );

  it("fails closed for parser-normalized credential paths", () => {
    expect(pathOnlyUrl("/visa/clock/zq-a/../zq-b")).toBe("[REDACTED]");
  });

  it("keeps an ordinary encoded path", () => {
    expect(pathOnlyUrl("https://host.example/x%20y/%E2%9C%93")).toBe(
      "https://host.example/x%20y/%E2%9C%93",
    );
  });

  it("uses the parser's per-character pathname witness", () => {
    expect(encodeAsParser("/visa/clock/{hash}")).toBe(
      "/visa/clock/" + new URL("http://x/a{").pathname.slice(2) + "hash%7D",
    );
    expect(pathOnlyUrl("/visa/clock/zq-a/../zq-b")).toBe("[REDACTED]");
  });

  it.each(TELEMETRY_PATH_CASES.filter(({ rules }) => !rules))(
    "applies the v3 path row through structured and error fields: $path",
    ({ path, expected }) => {
      const structured = expected.replace(
        TELEMETRY_PATH_REDACTION,
        "[REDACTED]",
      );
      expect(pathOnlyUrl(path)).toBe(structured);
      const out = scrubSentryUrls({ message: `GET ${path} failed` });
      expect(out.message).toBe(`GET ${structured} failed`);
    },
  );
});

describe("free-text addresses", () => {
  it.each(TELEMETRY_FREE_TEXT_CASES)(
    "cleans free-text row: $input",
    ({ input, expected }) => {
      const out = scrubSentryUrls({
        message: input,
        exception: { values: [{ value: input }] },
      });
      expect(out.message).toBe(expected);
      expect(out.exception.values[0].value).toBe(expected);
    },
  );
  it("masks address-shaped dot and encoded-separator tokens", () => {
    expect(
      redactFreeText(
        "GET https://host.example/visa/x/../clock/zq-synthetic failed",
      ),
    ).toBe("GET [REDACTED] failed");
    expect(redactFreeText("fetch /api/visa%2Fclock/zq-synthetic")).toBe(
      "fetch [REDACTED]",
    );
  });

  it("keeps ordinary prose and a public route", () => {
    expect(redactFreeText("see v1.2.3 and /clients/new")).toBe(
      "see v1.2.3 and /clients/new",
    );
  });
});
