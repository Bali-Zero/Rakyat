import { describe, it, expect } from "vitest";
import { normalizeDashboardRole, VALID_ROLES } from "../dashboard-role";

describe("normalizeDashboardRole", () => {
  it("returns 'zero' when isAdmin is true regardless of role string", () => {
    expect(normalizeDashboardRole("member", true)).toBe("zero");
    expect(normalizeDashboardRole(undefined, true)).toBe("zero");
    expect(normalizeDashboardRole("tax", true)).toBe("zero");
  });

  it("returns matched role for valid lowercase strings", () => {
    expect(normalizeDashboardRole("team", false)).toBe("team");
    expect(normalizeDashboardRole("tax", false)).toBe("tax");
    expect(normalizeDashboardRole("marketing", false)).toBe("marketing");
    expect(normalizeDashboardRole("accounting", false)).toBe("accounting");
  });

  it("is case-insensitive", () => {
    expect(normalizeDashboardRole("TAX", false)).toBe("tax");
    expect(normalizeDashboardRole("Marketing", false)).toBe("marketing");
    expect(normalizeDashboardRole("ACCOUNTING", false)).toBe("accounting");
  });

  it("falls back to 'team' for unknown roles", () => {
    expect(normalizeDashboardRole("consultant", false)).toBe("team");
    expect(normalizeDashboardRole("", false)).toBe("team");
    expect(normalizeDashboardRole(undefined, false)).toBe("team");
  });

  // Reorganisation 2026-11-09: a new job title must open the same widget as
  // the title it replaces. Guilt: a new title that maps differently fails.
  it.each([
    ["Supervisor", "Direktur"],
    ["Tax Lead", "Tax Supervisor"],
    ["Team Leader", "Tax Responsible"],
    ["Executive Consultant", "Senior Consultant"],
    ["Accounting", "HR & Accounting"],
  ])("%s and its replacement %s resolve to the same widget", (oldTitle, newTitle) => {
    expect(normalizeDashboardRole(newTitle, false)).toBe(
      normalizeDashboardRole(oldTitle, false),
    );
  });

  it("keeps the accounting widget for the HR & Accounting title, any case", () => {
    expect(normalizeDashboardRole("HR & Accounting", false)).toBe("accounting");
    expect(normalizeDashboardRole("hr & accounting", false)).toBe("accounting");
  });

  it("guilt control: a near-miss title does not borrow the accounting widget", () => {
    expect(normalizeDashboardRole("HR", false)).toBe("team");
    expect(normalizeDashboardRole("Finance & Client Services Coordinator", false)).toBe(
      "team",
    );
  });

  it.each(["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"])(
    "guilt control: the inherited key %s still resolves to 'team'",
    (raw) => {
      expect(normalizeDashboardRole(raw, false)).toBe("team");
    },
  );

  it("exports VALID_ROLES containing all 5 roles", () => {
    expect(VALID_ROLES).toContain("zero");
    expect(VALID_ROLES).toContain("team");
    expect(VALID_ROLES).toContain("tax");
    expect(VALID_ROLES).toContain("marketing");
    expect(VALID_ROLES).toContain("accounting");
    expect(VALID_ROLES).toHaveLength(5);
  });
});
