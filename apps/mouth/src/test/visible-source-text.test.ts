import { describe, expect, it } from "vitest";

import {
  HISTORICAL_PRICE_RE,
  PRICE_LITERAL_RE,
  visibleSourceText,
} from "./visible-source-text";

const textMatches = (source: string, pattern: RegExp) =>
  visibleSourceText(source).some((text) => pattern.test(text));

describe("visibleSourceText", () => {
  it("finds the four former JSX blind spots", () => {
    expect(
      textMatches(
        "<p>{label} https://example.test Open at any bank</p>",
        /any\s+bank/i,
      ),
    ).toBe(true);
    expect(textMatches("<p>passive{` `}income</p>", /passive\s+income/i)).toBe(
      true,
    );
    expect(
      textMatches("<p>passive{/* note */} income</p>", /passive\s+income/i),
    ).toBe(true);
    expect(
      textMatches(
        "<div onClick={() => { go(); }}>Rp 45 juta</div>",
        PRICE_LITERAL_RE,
      ),
    ).toBe(true);
  });

  it.each([
    "Rp 45.000.000",
    "Rp 35,000,000",
    "Rp. 45.000.000",
    "IDR 45 juta",
    "Rp 45 juta",
    "IDR 35M",
    "45M IDR",
    "35.000.000 IDR",
    "45.000.000 IDR",
    "45.000.000 rupiah",
    "45 juta",
    "45 jt",
    "IDR45000000",
  ])("finds price form %s as JSX, string, and template head", (price) => {
    expect(textMatches(`<p>${price}</p>`, PRICE_LITERAL_RE)).toBe(true);
    expect(
      textMatches(`const value = ${JSON.stringify(price)};`, PRICE_LITERAL_RE),
    ).toBe(true);
    expect(
      textMatches(`const value = \`${price} \${x}\`;`, PRICE_LITERAL_RE),
    ).toBe(true);
  });

  it("keeps template heads and historical figures in the same sweep", () => {
    expect(visibleSourceText("const x = `IDR 35M ${x} tail`; ")[0]).toBe(
      "IDR 35M",
    );
    for (const price of ["39.000.000", "35M"]) {
      expect(textMatches(`<p>${price}</p>`, PRICE_LITERAL_RE)).toBe(false);
      expect(textMatches(`<p>${price}</p>`, HISTORICAL_PRICE_RE)).toBe(true);
    }
  });

  it.each([
    "USD 130,000",
    "USD 3,000/month",
    "USD 50,000",
    "55+",
    "5 years",
    "1 year",
    "Pasal 113",
    "Permenkumham 22/2023",
    "IDR equivalent",
    "rupiah account",
    "E33F",
    "2026-11-24",
    "Rp",
    "4M visitors",
  ])("does not mistake %s for a price", (text) => {
    expect(PRICE_LITERAL_RE.test(text)).toBe(false);
    expect(HISTORICAL_PRICE_RE.test(text)).toBe(false);
  });

  it("omits comments, preserves URLs, and joins literal JSX children", () => {
    expect(
      visibleSourceText("// IDR 45.000.000\n/* passive income */"),
    ).toEqual([]);
    expect(
      visibleSourceText("<div>{/* <p>passive income</p> */}</div>"),
    ).toEqual([]);
    expect(
      visibleSourceText('const url = "https://example.test/a//b";'),
    ).toEqual(["https://example.test/a//b"]);
    expect(visibleSourceText("<p>Hello</p>")).toEqual(["Hello"]);
  });

  it.each(["9".repeat(32000), "9.".repeat(32000), `${"9 ".repeat(32000)}x`])(
    "matches a long numeric run linearly",
    (text) => {
      const started = performance.now();
      PRICE_LITERAL_RE.test(text);
      expect(performance.now() - started).toBeLessThan(250);
    },
  );
});
