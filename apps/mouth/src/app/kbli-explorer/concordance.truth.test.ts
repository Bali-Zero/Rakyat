import { describe, expect, it } from "vitest";

import { KBLI_CONCORDANCE_2025 } from "./concordance";

describe("KBLI_CONCORDANCE_2025 normative truth", () => {
  it("does not mark active KBLI 70209 as REVOKED", () => {
    expect(KBLI_CONCORDANCE_2025["70209"]?.status).not.toBe("REVOKED");
  });

  it("never advises PT PMA migration into 0%-PMA codes 55203 or 55106", () => {
    for (const [code, entry] of Object.entries(KBLI_CONCORDANCE_2025)) {
      const action = entry.action.toLowerCase();
      expect(action, `${code} must not advise migrating to 55203`).not.toMatch(
        /migrate to 55203|move to 55203/,
      );
      expect(action, `${code} must not advise migrating to 55106`).not.toMatch(
        /migrate to 55106|move to 55106/,
      );
    }
  });
});
