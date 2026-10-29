import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(
    join(process.cwd(), "src/app/(visa-oracle)/visa-oracle", rel),
    "utf8",
  );

describe("/visa-oracle canonical points at itself, not the root", () => {
  const layout = read("layout.tsx");

  it("declares alternates.canonical https://balizero.com/visa-oracle", () => {
    expect(layout).toMatch(
      /canonical:\s*"https:\/\/balizero\.com\/visa-oracle"/,
    );
  });

  it("does not point the canonical at the root domain", () => {
    expect(layout).not.toMatch(/canonical:\s*"https:\/\/balizero\.com\/?"/);
  });

  it("keeps its title (positive control)", () => {
    expect(layout).toMatch(/title:\s*"Visa Oracle"/);
  });
});

describe("/visa-oracle/privacy canonical points at itself, not the root", () => {
  const layout = read("privacy/layout.tsx");

  it("declares alternates.canonical https://balizero.com/visa-oracle/privacy", () => {
    expect(layout).toMatch(
      /canonical:\s*"https:\/\/balizero\.com\/visa-oracle\/privacy"/,
    );
  });

  it("does not point the canonical at the root domain", () => {
    expect(layout).not.toMatch(/canonical:\s*"https:\/\/balizero\.com\/?"/);
  });

  it("keeps its title (positive control)", () => {
    expect(layout).toMatch(/title:\s*"Visa Oracle Privacy Policy"/);
  });
});
