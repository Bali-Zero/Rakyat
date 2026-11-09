import { describe, expect, it } from "vitest";
import { metadata as rootMetadata } from "../layout";
import { metadata } from "./layout";

/**
 * kita.balizero.com/login inherited the root layout's homepage canonical
 * (`https://balizero.com`) and had no robots meta of its own. Next replaces
 * a parent's `robots` / `alternates` wholesale when a child segment declares
 * them, so declaring them here is what the served page carries.
 */
describe("kita /login metadata", () => {
  it("positive control: the root layout still points canonical at balizero.com", () => {
    expect(String(rootMetadata.alternates?.canonical)).toMatch(
      /^https:\/\/balizero\.com/,
    );
  });

  it("is noindex, nofollow", () => {
    expect(metadata.robots).toMatchObject({ index: false, follow: false });
  });

  it("does not point canonical or hreflang at balizero.com", () => {
    expect(metadata.alternates).toBeDefined();
    expect(metadata.alternates?.canonical ?? null).toBeNull();
    expect(JSON.stringify(metadata.alternates)).not.toMatch(/balizero\.com/);
  });
});
