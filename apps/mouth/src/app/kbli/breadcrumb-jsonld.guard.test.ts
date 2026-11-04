import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const ROOT = read("./page.tsx");
const CODE = read("./[code]/page.tsx");

describe("/kbli root emits BreadcrumbList JSON-LD", () => {
  it("root page renders KBLIBreadcrumbJsonLd", () => {
    expect(ROOT).toMatch(/<KBLIBreadcrumbJsonLd\b/);
  });

  it("root breadcrumb is Home -> KBLI Navigator with the canonical URLs", () => {
    expect(ROOT).toMatch(
      /name:\s*"Home",\s*url:\s*"https:\/\/balizero\.com"\s*\}/,
    );
    expect(ROOT).toMatch(
      /name:\s*"KBLI Navigator",\s*url:\s*"https:\/\/balizero\.com\/kbli"\s*\}/,
    );
  });

  it("the code page already renders it (positive control)", () => {
    expect(CODE).toMatch(/<KBLIBreadcrumbJsonLd\b/);
  });
});
