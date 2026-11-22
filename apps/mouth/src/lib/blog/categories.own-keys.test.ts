import { describe, expect, it } from "vitest";
import { CATEGORY_MAP, normalizeCategory } from "./categories";

describe("normalizeCategory own keys", () => {
  it.each([
    "constructor",
    "toString",
    "valueOf",
    "hasOwnProperty",
    "__proto__",
  ])("takes the fallback for the inherited name %s", (name) => {
    const result = normalizeCategory(name);
    expect(typeof result).toBe("string");
    expect(result).toBe("living");
  });

  it("maps every real category as before", () => {
    for (const [raw, expected] of Object.entries(CATEGORY_MAP)) {
      expect(normalizeCategory(raw)).toBe(expected);
    }
    expect(normalizeCategory("tax-legal")).toBe("taxes");
    expect(normalizeCategory("news")).toBe("business");
  });

  it("takes the fallback for an ordinary unknown string", () => {
    expect(normalizeCategory("no-such-folder")).toBe("living");
    expect(normalizeCategory("")).toBe("living");
  });
});
