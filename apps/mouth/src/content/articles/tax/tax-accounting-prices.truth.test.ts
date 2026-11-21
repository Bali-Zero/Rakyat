import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const TAX_DIR = path.join(process.cwd(), "src/content/articles/tax");
const ARTICLES_DIR = path.join(process.cwd(), "src/content/articles");

const SYNCED_FAMILIES = [
  "tax/pph-25-monthly-installments-guide",
  "tax/coretax-login-errors-fixes-2026",
  "tax/coretax-npwp16-vs-npwp15-foreigners",
  "tax/coretax-vs-djp-online-what-changed",
  "tax/tax-amnesty-indonesia-history",
  "tax/tax-loss-carryforward-indonesia",
  "tax/thin-capitalization-indonesia",
  "tax/transfer-pricing-indonesia-guide",
  "tax/pph-29-annual-settlement-guide",
  "tax/corporate-income-tax",
  "tax/tax-calendar-indonesia",
  "tax/vat-ppn-guide",
  "business/pt-pma-capital-requirements",
  "business/capital-requirements-guide",
  "business/foreign-company-incorporation-steps",
  "immigration/shareholder-kitas-requirements",
  "immigration/e33g-remote-worker-visa-guide",
];

function bodyOf(raw: string): string {
  return raw.replace(/^---\s*\n([\s\S]*?)\n---\s*\n/, "");
}

const FORBIDDEN: { label: string; pattern: RegExp }[] = [
  { label: "Accounting Basic", pattern: /\bAccounting Basic\b/i },
  { label: "Accounting Premium", pattern: /\bAccounting Premium\b/i },
  { label: "Akuntansi Dasar", pattern: /\bAkuntansi Dasar\b/i },
  {
    label: "invented IDR 1.5M accounting package",
    pattern: /IDR\s*1[.,]500[.,]000\s*\/\s*(?:month|bulan|mese|mo)/i,
  },
  {
    label: "invented IDR 3.0M Accounting Premium package",
    pattern:
      /(?:Accounting|Akuntansi|Pacchetto)[^\n.]{0,80}IDR\s*3[.,]000[.,]000\s*\/\s*(?:month|bulan|mese|mo)/i,
  },
  {
    label: "invented IDR 5M CoreTax migration package",
    pattern:
      /(?:CoreTax migration|migrasi CoreTax|migrazione[^.\n]{0,40}CoreTax)[^.\n]{0,80}IDR\s*5[.,]000[.,]000/i,
  },
  {
    label: "invented Bali Zero IDR 15M/yr TP documentation price",
    pattern:
      /(?:Bali Zero|our TP documentation|dokumentasi TP kami|nostro servizio di documentazione TP|Transfer pricing documentation[^.\n]{0,80}:)[^\n]{0,100}IDR\s*15[.,]000[.,]000\s*\/\s*(?:year|tahun|anno)/i,
  },
  {
    label: "false PT PMA 0.5% UMKM eligibility claim (EN)",
    pattern:
      /(?:PT PMA companies became eligible|If your PT PMA qualifies for the 0\.5% UMKM)/i,
  },
  {
    label: "false PT PMA 0.5% UMKM eligibility claim (ID)",
    pattern:
      /(?:Perusahaan PT PMA menjadi memenuhi syarat|Jika PT PMA Anda memenuhi syarat untuk tarif pajak final UMKM)/i,
  },
  {
    label: "false PT PMA 0.5% UMKM eligibility claim (IT)",
    pattern:
      /(?:società PT PMA sono diventate ammissibili|Se la tua PT PMA ha diritto all'aliquota finale UMKM)/i,
  },
];

describe("tax articles SSOT accounting prices & parity", () => {
  it("contains zero invented accounting/CoreTax/TP pricing packages or false PT PMA UMKM claims across EN/ID/IT MDX files", () => {
    const files = fs
      .readdirSync(TAX_DIR)
      .filter((f) => f.endsWith(".mdx"))
      .sort();
    expect(files.length).toBeGreaterThan(20);

    const violations: string[] = [];
    for (const file of files) {
      const content = fs.readFileSync(path.join(TAX_DIR, file), "utf8");
      for (const { label, pattern } of FORBIDDEN) {
        if (pattern.test(content)) {
          violations.push(`${file}: matched ${label}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it("keeps source_sha256 synchronized with EN bodies across all 17 edited article families", () => {
    const mismatches: string[] = [];
    for (const family of SYNCED_FAMILIES) {
      const enRaw = fs.readFileSync(
        path.join(ARTICLES_DIR, `${family}.mdx`),
        "utf8",
      );
      const expected = crypto
        .createHash("sha256")
        .update(bodyOf(enRaw))
        .digest("hex");
      for (const locale of ["id", "it", "fr", "ru"]) {
        const twinPath = path.join(ARTICLES_DIR, `${family}.${locale}.mdx`);
        if (!fs.existsSync(twinPath)) continue;
        const twinRaw = fs.readFileSync(twinPath, "utf8");
        const m = twinRaw.match(/^source_sha256:\s*"([0-9a-f]{64})"/m);
        const actual = m?.[1] ?? "MISSING";
        if (actual !== expected) {
          mismatches.push(`${family}.${locale}.mdx: ${actual} !== ${expected}`);
        }
      }
    }
    expect(mismatches).toEqual([]);
  });
});
