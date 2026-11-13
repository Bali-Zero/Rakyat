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
});
