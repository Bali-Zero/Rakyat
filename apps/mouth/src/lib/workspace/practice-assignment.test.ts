import { describe, expect, it } from "vitest";
import { canAssignPracticeToAnyone } from "./practice-assignment";

describe("canAssignPracticeToAnyone", () => {
  it("lets an admin assign whatever the role says", () => {
    expect(canAssignPracticeToAnyone(true, "Consultant")).toBe(true);
    expect(canAssignPracticeToAnyone(true, null)).toBe(true);
  });

  it.each(["Board Member", "board member", "  Board Member "])(
    "lets the role %j assign to anyone",
    (role) => {
      expect(canAssignPracticeToAnyone(false, role)).toBe(true);
    },
  );

  it.each([
    "Tax Consultant",
    "Consultant",
    "Reception",
    "Board",
    "",
    null,
    undefined,
  ])("keeps the role %j on self only", (role) => {
    expect(canAssignPracticeToAnyone(false, role)).toBe(false);
  });
});
