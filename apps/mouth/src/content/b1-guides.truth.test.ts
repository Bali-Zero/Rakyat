import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";
import { describe, expect, it } from "vitest";

// Primary sources rechecked 2026-11-09: Ngurah Rai /Layanan%20WNA/ and
// Jakarta Pusat /layanan/warga-negara-asing-wna/visa-republik-indonesia/
// b1-visa-saat-kedatangan-wisata and c1-visa-wisata.
const slugs = [
  "e-voa-electronic-visa-on-arrival-guide",
  "tourist-visa-extension-indonesia-guide",
];
const locales = ["", ".id", ".it", ".fr", ".ru"];
const root = path.join(process.cwd(), "src/content/articles/immigration");
const files = slugs.flatMap((slug) =>
  locales.map((locale) => `${slug}${locale}.mdx`),
);
const normalize = (text: string) =>
  text.replace(/[*_]/g, "").replace(/\s+/g, " ");
// Entity-scoped: legitimate C1 60-day initial stays and 180-day ceilings survive.
const wrongInitial =
  /(?:initial E-VOA|E-VOA (?:Awal|iniziale)|E-VOA initial|первоначальн[а-яё]* E-VOA)\s*:\s*60/iu;
const wrongCeiling =
  /(?:E-?VOA|B1)[^.!?\n|]{0,100}(?:120|\b65)\s*(?:days|hari|giorni|jours|дн)/iu;
const wrongCount =
  /(?:E-?VOA|B1)[^.!?\n|]{0,65}(?:extendable twice|two extensions|due proroghe|dua kali|deux prolongations|два продления)/iu;
const wrongB1Initial =
  /(?:B1|E-?VOA)[^.!?\n|]{0,35}(?:initial (?:stay|validity)|soggiorno iniziale|masa tinggal awal|séjour initial)\s*:\s*60/iu;
const rules = [wrongInitial, wrongB1Initial, wrongCeiling, wrongCount];

describe("B1 guide factual regression guard", () => {
  it.each([
    "Initial E-VOA: 60 days",
    "B1 initial stay: 60 days",
    "E-VOA initial validity: 60 days",
    "B1 soggiorno iniziale: 60 giorni",
    "E-VOA Awal: 60 hari",
    "E-VOA iniziale: 60 giorni",
    "E-VOA initial : 60 jours",
    "E-VOA allows 120 days total",
    "E-VOA estendibile a 120 giorni",
    "E-VOA extendable twice",
    "B1 продлевается: два продления",
  ])("rejects the old claim: %s", (sample) => {
    expect(rules.some((rule) => rule.test(normalize(sample)))).toBe(true);
  });
  it.each([
    "B1: 30 days initially, one 30-day extension, maximum 60 days.",
    "C1: initial 60 days, maximum 180 days with approved extensions.",
    "B1: 30 giorni, una proroga di 30 giorni, 60 giorni totali.",
  ])("spares the legitimate comparison: %s", (sample) => {
    expect(rules.some((rule) => rule.test(normalize(sample)))).toBe(false);
  });
  it.each(files)("%s has correct metadata, FAQ and body", (file) => {
    const raw = fs.readFileSync(path.join(root, file), "utf8");
    const { data, content } = matter(raw);
    for (const rule of rules) expect(normalize(raw)).not.toMatch(rule);
    expect(content.length).toBeGreaterThan(2000);
    expect(data.updatedAt).toBe("2026-11-09");
    for (const key of ["description", "excerpt", "seoDescription"]) {
      expect(data[key], `${file}: ${key}`).toMatch(/30/);
      expect(data[key], `${file}: ${key}`).not.toMatch(/120/);
    }
    expect(data.aiOptimization.answerSnippet).toMatch(/30/);
    expect(data.aiOptimization.answerSnippet).toMatch(/60/);
    expect(data.faq.length).toBeGreaterThanOrEqual(3);
    expect(JSON.stringify(data.faq)).not.toMatch(/120/);
    expect(content).toContain("https://ngurahrai.imigrasi.go.id/");
    expect(content).toContain("C1");
  });
});

// A changed English source must not leave old translation provenance behind.
// Hash the same body bytes used by the existing E28 translation guard.
describe("B1 guide translation provenance", () => {
  for (const slug of slugs) {
    it(`${slug} keeps five complete, current language versions`, async () => {
      const { createHash } = await import("node:crypto");
      const bodyOf = (raw: string) =>
        raw.replace(/^---\s*\n([\s\S]*?)\n---\s*\n/, "");
      const english = fs.readFileSync(path.join(root, `${slug}.mdx`), "utf8");
      const enBody = bodyOf(english);
      const digest = createHash("sha256").update(enBody, "utf8").digest("hex");
      const enData = matter(english).data;
      for (const locale of locales.filter(Boolean)) {
        const raw = fs.readFileSync(
          path.join(root, `${slug}${locale}.mdx`),
          "utf8",
        );
        const data = matter(raw).data;
        expect(data.locale).toBe(locale.slice(1));
        expect(data.source_sha256).toBe(digest);
        expect(data.author.role).not.toBe(enData.author.role);
        expect(data.aiOptimization.primaryQuestion).not.toBe(
          enData.aiOptimization.primaryQuestion,
        );
        expect(bodyOf(raw).match(/^## /gm)?.length).toBe(
          enBody.match(/^## /gm)?.length,
        );
        for (const field of [
          "title",
          "description",
          "excerpt",
          "seoTitle",
          "seoDescription",
          "coverImageAlt",
        ]) {
          expect(data[field], `${locale}: ${field} untranslated`).not.toBe(
            enData[field],
          );
        }
      }
    });
  }
});
