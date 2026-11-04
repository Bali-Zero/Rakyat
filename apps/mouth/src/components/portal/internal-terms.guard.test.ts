import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

// Client-facing portal copy must not leak staff vocabulary
// ("team leader", "CRM", "Practices", a raw role value).
const SURFACES = {
  "process/page.tsx": read("../../app/portal/(authenticated)/process/page.tsx"),
  "family/page.tsx": read("../../app/portal/(authenticated)/family/page.tsx"),
  "vault/VaultSidebar.tsx": read("./vault/VaultSidebar.tsx"),
  "settings/AccountSettings.tsx": read("./settings/AccountSettings.tsx"),
};

describe("portal copy uses client words, not staff words", () => {
  it("the empty Process page names the case officer, not a team leader", () => {
    const src = SURFACES["process/page.tsx"];
    expect(src).not.toMatch(/team\s+leader/i);
    expect(src).toMatch(/case officer creates a process/);
  });

  it("the empty Family page does not send the client to the CRM", () => {
    const src = SURFACES["family/page.tsx"];
    expect(src).not.toMatch(/\bCRM\b/);
    expect(src).toMatch(/Ask your case officer to add them/);
  });

  it("the Vault filter heading is Categories, not Practices", () => {
    const src = SURFACES["vault/VaultSidebar.tsx"];
    expect(src).not.toMatch(/^\s*Practices\s*$/m);
    expect(src).toMatch(/^\s*Categories\s*$/m);
  });

  it("Account settings no longer show the internal role", () => {
    const src = SURFACES["settings/AccountSettings.tsx"];
    expect(src).not.toMatch(/label="Role"/);
    expect(src).toMatch(/label="Email"/);
  });
});
