import { describe, expect, it } from "vitest";
import { getAllCodes, getCode } from "@/lib/kbli-data";
import { resolveKbliCodes } from "./kbli-resolve";

describe("resolveKbliCodes against the real navigator dataset", () => {
  it("a 2025 code resolves to the navigator's own title", () => {
    const code = getAllCodes()[0].code;
    const r = resolveKbliCodes([code])[code];
    expect(r.inNavigator).toBe(true);
    expect(r.title).toBe(getCode(code)!.titleEn);
    expect(r.successors).toEqual([]);
  });

  it("a code the navigator lacks is flagged and takes its successor from the BPS crosswalk", () => {
    const r = resolveKbliCodes(["55199"])["55199"];
    expect(getCode("55199")).toBeUndefined();
    expect(r.inNavigator).toBe(false);
    expect(r.title).toBeUndefined();
    expect(r.successors.map((s) => s.code)).toContain("55209");
    for (const s of r.successors)
      expect(s.title).toBe(getCode(s.code)!.titleEn);
  });

  it("INNOCENCE: a code with no crosswalk entry has no suggestion, none is invented", () => {
    const r = resolveKbliCodes(["99999"])["99999"];
    expect(r).toEqual({ inNavigator: false, successors: [] });
  });
});
