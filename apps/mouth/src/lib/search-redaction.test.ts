import { describe, it, expect } from "vitest";
import { pathOnly, redactSearchParams } from "./search-redaction";

const VALUE = "zetaquorum-fixture-search";

describe("redactSearchParams", () => {
  it.each([
    [`/clients?search=${VALUE}&page=2`, "/clients?search=[REDACTED]&page=2"],
    [`/clients?page=2&Q=${VALUE}`, "/clients?page=2&Q=[REDACTED]"],
    [`/clients?se%61rch=${VALUE}`, "/clients?se%61rch=[REDACTED]"],
    [`/clients?%73earch=${VALUE}`, "/clients?%73earch=[REDACTED]"],
    [
      `GET https://x/y?a=1;q=${VALUE} done`,
      "GET https://x/y?a=1;q=[REDACTED] done",
    ],
  ])("redacts the value of %s", (input, expected) => {
    expect(redactSearchParams(input)).toBe(expected);
    expect(redactSearchParams(input)).not.toContain(VALUE);
  });

  it("leaves look-alike keys, other params and plain text alone", () => {
    const text =
      "https://x/y?research=1&frequency=2&month=2026-11 and q-learning, search results";
    expect(redactSearchParams(text)).toBe(text);
    expect(redactSearchParams("bad %E0%A4%A param ?%E0%A4%A=1")).toBe(
      "bad %E0%A4%A param ?%E0%A4%A=1",
    );
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
