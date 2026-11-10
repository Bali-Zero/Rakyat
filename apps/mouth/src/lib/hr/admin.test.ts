import { describe, expect, it } from "vitest";
import { isHRAdmin } from "./admin";

describe("isHRAdmin mirrors the backend HR-admin set", () => {
  it("admits the Direktur and the HR specialist, case-insensitively", () => {
    expect(isHRAdmin({ email: "adit@balizero.com", role: "member" })).toBe(true);
    expect(isHRAdmin({ email: "ADIT@balizero.com", role: "member" })).toBe(true);
    expect(isHRAdmin({ email: "ruslana@balizero.com", role: "member" })).toBe(true);
  });

  it("refuses a setup member, a tax member, the retired login and no profile", () => {
    for (const email of [
      "krisna@balizero.com",
      "kadek.tax@balizero.com",
      "angel.tax@balizero.com",
      "tax@balizero.com",
    ]) {
      expect(isHRAdmin({ email, role: "member" }), email).toBe(false);
    }
    expect(isHRAdmin(null)).toBe(false);
  });
});
