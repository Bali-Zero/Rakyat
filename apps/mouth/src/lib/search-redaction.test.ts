import { describe, it, expect } from "vitest";
import { pathOnly, stripUrlQueries } from "./search-redaction";

const VALUE = "zetaquorum-fixture-search";
const TAIL = "zetaquorum-fixture-tail";

// Every shape a search value can take after the "?" or "#" of a URL.
const HOSTILE = [
  `search=${VALUE}`,
  `q=${VALUE}&page=2`,
  `se%61rch=${VALUE}`,
  `%73EARCH=${VALUE}`,
  `a=1;q=${VALUE}`,
  `q=prefix;${TAIL}`,
  `q=prefix%3B${TAIL}`,
  `q=a%26b%3D${TAIL}`,
  `x=1#q=${VALUE}`,
];

describe("stripUrlQueries", () => {
  it.each(HOSTILE)("cuts the query of ?%s", (query) => {
    const out = stripUrlQueries(`GET https://x/clients?${query} -> 200`);
    expect(out).toBe("GET https://x/clients -> 200");
    expect(out).not.toContain(VALUE);
    expect(out).not.toContain(TAIL);
  });

  it("cuts a fragment and a relative URL's query", () => {
    expect(stripUrlQueries(`/process#q=${VALUE}`)).toBe("/process");
    expect(stripUrlQueries(`/process?search=${VALUE}`)).toBe("/process");
  });

  it("leaves paths and plain text alone", () => {
    const text =
      "https://x/api/crm/practices/12/documents issue #123 and q-learning, search results";
    expect(stripUrlQueries(text)).toBe(text);
  });
});

describe("pathOnly", () => {
  it("drops the query and the fragment", () => {
    expect(pathOnly(`https://kita.example/clients?q=${VALUE}#top`)).toBe(
      "/clients",
    );
    expect(pathOnly("/process?search=x")).toBe("/process");
  });

  it("returns null for nothing", () => {
    expect(pathOnly(null)).toBeNull();
    expect(pathOnly("")).toBeNull();
  });
});
