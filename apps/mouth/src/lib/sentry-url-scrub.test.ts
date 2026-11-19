import { describe, it, expect } from "vitest";
import { scrubSentryUrls } from "./sentry-url-scrub";

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
});
