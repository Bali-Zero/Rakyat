import { describe, expect, it } from "vitest";
import type { BonusPracticeType } from "@/types/hr";
import { groupOfCode, groupPracticeTypes } from "./practice-groups";

const t = (
  code: string,
  name: string,
  group: BonusPracticeType["group"],
): BonusPracticeType => ({ code, name, category: null, group });

const TYPES = [
  t("tax_fake_report", "Annual report", "tax"),
  t("company_fake_pt", "Company set up", "company"),
  t("visa_fake_b", "Beta visa", "visa"),
  t("visa_fake_a", "Alpha visa", "visa"),
];

describe("groupPracticeTypes", () => {
  it("always returns the four groups in order, sorted by name, empty ones kept", () => {
    const groups = groupPracticeTypes(TYPES);
    expect(groups.map((g) => g.label)).toEqual([
      "Visa & KITAS",
      "Company",
      "Tax",
      "Other",
    ]);
    expect(groups[0].items.map((i) => i.name)).toEqual([
      "Alpha visa",
      "Beta visa",
    ]);
    expect(groups[3].items).toEqual([]);
  });

  it("filters by name or by code with underscores read as spaces", () => {
    expect(
      groupPracticeTypes(TYPES, "annual").flatMap((g) => g.items),
    ).toHaveLength(1);
    expect(
      groupPracticeTypes(TYPES, "company fake").flatMap((g) => g.items),
    ).toHaveLength(1);
    expect(
      groupPracticeTypes(TYPES, "zzz").flatMap((g) => g.items),
    ).toHaveLength(0);
  });
});

describe("groupOfCode", () => {
  it("uses the catalogue, then falls back on the code prefix for retired types", () => {
    expect(groupOfCode("tax_fake_report", TYPES)).toBe("tax");
    expect(groupOfCode("tax_retired", [])).toBe("tax");
    expect(groupOfCode("company_x", [])).toBe("company");
    expect(groupOfCode("kitap_application", [])).toBe("visa");
    expect(groupOfCode("misc", [])).toBe("other");
  });
});
