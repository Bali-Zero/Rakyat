import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const TAX_DIR = path.join(process.cwd(), "src/content/articles/tax");

// The NPWP Personal + Coretax Activation service costs IDR 1,000,000 with a KITAS and
// IDR 1,500,000 without one (owner ruling 2026-11-27). Every public copy says both.
const SLUGS = [
  "coretax-npwp16-vs-npwp15-foreigners",
  "coretax-login-errors-fixes-2026",
  "coretax-vs-djp-online-what-changed",
];
const LANGS: { suffix: string; withKitas: RegExp; withoutKitas: RegExp }[] = [
  {
    suffix: "",
    withKitas: /NPWP Personal[^.]*?IDR 1,000,000 with a KITAS/,
    withoutKitas: /IDR 1,500,000 without a KITAS/,
  },
  {
    suffix: ".it",
    withKitas: /NPWP[^.]*?IDR 1\.000\.000 con KITAS/,
    withoutKitas: /IDR 1\.500\.000 senza KITAS/,
  },
  {
    suffix: ".id",
    withKitas: /NPWP Personal[^.]*?IDR 1\.000\.000 dengan KITAS/,
    withoutKitas: /IDR 1\.500\.000 tanpa KITAS/,
  },
];

function split(raw: string): { front: string; body: string } {
  const match = raw.match(/^---\s*\n([\s\S]*?)\n---\s*\n/);
  return match
    ? { front: match[1], body: raw.slice(match[0].length) }
    : { front: "", body: raw };
}

const read = (slug: string, suffix: string) =>
  split(fs.readFileSync(path.join(TAX_DIR, `${slug}${suffix}.mdx`), "utf-8"));

describe("every public copy of the NPWP personal price names its KITAS case", () => {
  for (const slug of SLUGS) {
    for (const lang of LANGS) {
      it(`${slug}${lang.suffix} states both prices`, () => {
        const { body } = read(slug, lang.suffix);
        expect(body).toMatch(lang.withKitas);
        expect(body).toMatch(lang.withoutKitas);
      });
    }
  }

  // The hourly translator re-translates a copy whose recorded source_sha256 differs from the
  // sha256 of the English body. These copies were edited by hand with the English source, so
  // they must carry the new digest, or the translator would overwrite them.
  for (const slug of SLUGS) {
    for (const suffix of [".it", ".id"]) {
      it(`${slug}${suffix} is stamped with the digest of the current English body`, () => {
        const digest = crypto
          .createHash("sha256")
          .update(read(slug, "").body, "utf-8")
          .digest("hex");
        const stamp = read(slug, suffix).front.match(
          /^source_sha256:\s*"?([0-9a-f]{64})"?\s*$/m,
        );
        expect(stamp?.[1]).toBe(digest);
      });
    }
  }
});
