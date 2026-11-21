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

// The same wrong rule ("60 days, extendable to 120") also stood in two other
// immigration article families. Both run through the same four patterns; the
// comparison article additionally pins its metadata, its money and its
// translation provenance. Money: the only amounts it may state are Bali Zero's
// catalogue prices for the B1 VOA and its extension, and zero for visa-free.
const otherSlugs = [
  "kitas-cancellation-company-closure",
  "visa-free-vs-evoa-indonesia-comparison",
];
const comparison = "visa-free-vs-evoa-indonesia-comparison";
const otherFiles = otherSlugs.flatMap((slug) =>
  locales.map((locale) => `${slug}${locale}.mdx`),
);
const comparisonFiles = locales.map((locale) => `${comparison}${locale}.mdx`);
// Money is read where it is written as money, a figure next to a currency
// marker, so a pixel size or a year in an unrelated field is not an amount.
const gap = "[ \\u00a0\\u202f]*";
const figure = "(\\d{1,3}(?:[ .,\\u00a0\\u202f]\\d{3})+|\\d+)";
const scale = "(K\\b|M\\b|million|juta|milion[ei]|млн)?";
const moneyBefore = new RegExp(
  `\\b(?:IDR|Rp\\.?)${gap}${figure}${gap}${scale}`,
  "giu",
);
const moneyAfter = new RegExp(
  `${figure}${gap}${scale}${gap}(?:IDR\\b|Rp\\b|rupiah|rupie|roupies|рупи)`,
  "giu",
);
const money = (text: string) =>
  [...text.matchAll(moneyBefore), ...text.matchAll(moneyAfter)].map((m) => ({
    value: Number(m[1].replace(/[^0-9]/g, "")),
    unit: m[2],
  }));
// In the comparison article the visa is named by the section heading or the
// table column, so three old shapes carry no "E-VOA" or "B1" on their line and
// pass the four patterns: an initial stay of 60, an extension count of two, a
// second extension window (days 91-120). Here the entity is the file, read
// line by line. "Initial 30" after the 60 is the corrected rule and is spared.
// A negated sentence ("cannot be extended twice") would trip: say it as the
// article does, "there is no second extension".
// The rule is the B1's, so a line about another visa is not judged by it: the
// line's own visa name decides, and a line that names none takes its heading's.
// A line or heading that names no visa is read as the B1, the article's subject.
const b1Visa = /\bB1\b|\bE-?VOA\b|\bVOA\b|visa on arrival/iu;
const otherVisa = /\b[CDE]\d{1,2}[A-Z]?\b|\bB211[AB]?\b|\bKITA[SP]\b/u;
const aboutOtherVisa = (text: string) =>
  otherVisa.test(text) && !b1Visa.test(text);
const initialWord = "(?:\\binitial|\\binizial|\\bawal|начальн)";
const initialSixty = new RegExp(
  [
    `\\b60\\b[^.!?|,;:0-9]{0,22}${initialWord}(?![^.!?|]{0,25}\\b30\\b)`,
    `${initialWord}[^.!?|,;]{0,25}:\\s*60\\b`,
    `${initialWord}[a-zа-яё]*\\s+60\\b`,
  ].join("|"),
  "iu",
);
const initialLabel = new RegExp(initialWord, "iu");
// "Twice" counts only when it is bound to the extension: the word itself
// ("two extensions"), an extension word next to it ("extendable twice"), or a
// table row whose label asks about the extension. "Check the address twice
// before requesting an extension" is about the address.
const twoExtensions =
  /\b(?:2|two) extensions\b|\bdue proroghe\b|\bdeux prolongations\b|два продления/iu;
const twice =
  "(?:\\btwice\\b|\\bdua kali\\b|\\bdue volte\\b|\\bdeux fois\\b|дважды)";
const extension =
  "(?:exten[ds]|perpanjang|prorog|estensi|estendi|prolong|продл)";
const twiceWord = new RegExp(twice, "iu");
const extensionWord = new RegExp(extension, "iu");
const extendedTwice = new RegExp(
  `${extension}\\S*\\s+(?:\\S+\\s+)?${twice}|${twice}\\s+(?:\\S+\\s+)?${extension}`,
  "iu",
);
const secondWindow = /\b91\s*[-–]\s*120\b/;
const restatesOldRule = (rawLine: string, heading = "") => {
  const line = normalize(rawLine).trim();
  if (aboutOtherVisa(line)) return false;
  if (!b1Visa.test(line) && aboutOtherVisa(normalize(heading))) return false;
  if (secondWindow.test(line)) return true;
  if (twoExtensions.test(line) || extendedTwice.test(line)) return true;
  if (!line.startsWith("|")) return initialSixty.test(line);
  const [, label = "", ...cells] = line.split("|");
  if (extensionWord.test(label) && cells.some((c) => twiceWord.test(c)))
    return true;
  return initialLabel.test(label) && cells.some((c) => /^\s*60\b/.test(c));
};
// The file is read top to bottom so each line carries its nearest heading.
const linesRestatingOldRule = (raw: string) => {
  let heading = "";
  return raw.split("\n").filter((line) => {
    if (/^#{1,6}\s/.test(line)) heading = line;
    return restatesOldRule(line, heading);
  });
};

describe("B1 claim guard on the KITAS-closure and comparison articles", () => {
  it.each(otherFiles)("%s carries none of the four wrong patterns", (file) => {
    const raw = fs.readFileSync(path.join(root, file), "utf8");
    for (const rule of rules) expect(normalize(raw)).not.toMatch(rule);
  });
  it.each([
    "**Tourist visa (E-VOA):** - Available immediately after exit - 60 days stay (extendable to 120 days)",
    "**Visa turistik (E-VOA):** - Masa tinggal 60 hari (dapat diperpanjang hingga 120 hari)",
    "E-VOA: 60 jours (prolongeable jusqu’à 120 jours)",
  ])("rejects the multi-line KITAS shape: %s", (sample) => {
    expect(rules.some((rule) => rule.test(normalize(sample)))).toBe(true);
  });
  it.each(comparisonFiles)("%s metadata, FAQ and money are correct", (file) => {
    const raw = fs.readFileSync(path.join(root, file), "utf8");
    const { data } = matter(raw);
    // A floor, not a pin: a later editorial update must not break this.
    const updated = new Date(data.updatedAt).toISOString().slice(0, 10);
    expect(updated >= "2026-11-11", `${file}: updatedAt ${updated}`).toBe(true);
    for (const key of ["description", "excerpt", "seoDescription"]) {
      expect(data[key], `${file}: ${key}`).toMatch(/30/);
      expect(data[key], `${file}: ${key}`).not.toMatch(/120/);
    }
    expect(data.aiOptimization.answerSnippet).toMatch(/30/);
    expect(data.aiOptimization.answerSnippet).toMatch(/60/);
    expect(data.faq.length).toBeGreaterThanOrEqual(3);
    expect(JSON.stringify(data.faq)).not.toMatch(/120/);
    expect(raw).not.toMatch(/\bUSD\b|\$\s?\d|500\s*K\b/i);
    const amounts = money(raw);
    for (const { value, unit } of amounts) {
      expect(unit, `${file}: ${value} ${unit}`).toBeUndefined();
      expect([0, 750000, 850000], `${file}: amount ${value}`).toContain(value);
    }
    expect(amounts.map((amount) => amount.value)).toContain(750000);
    expect(amounts.map((amount) => amount.value)).toContain(850000);
  });
  it.each(comparisonFiles)(
    "%s names only related articles that exist",
    (file) => {
      const { data } = matter(fs.readFileSync(path.join(root, file), "utf8"));
      const articles = path.dirname(root);
      const categories = fs.readdirSync(articles);
      for (const slug of data.relatedArticles ?? []) {
        const found = categories.some((dir) =>
          fs.existsSync(path.join(articles, dir, `${slug}.mdx`)),
        );
        expect(found, `${file}: ${slug}`).toBe(true);
      }
    },
  );
  it("reads money only where it is written as money", () => {
    expect(money("cover image 1,000 pixels wide, checked in 2026")).toEqual([]);
    expect(money("IDR 500,000 (~USD 32)")[0].value).toBe(500000);
    expect(money("Rp 750.000, IDR 850 000").map((m) => m.value)).toEqual([
      750000, 850000,
    ]);
    expect(money("IDR 500K")[0]).toEqual({ value: 500, unit: "K" });
    expect(money("IDR 1 million")[0]).toEqual({ value: 1, unit: "million" });
    expect(money("500.000 rupiah")[0].value).toBe(500000);
  });
  it.each([
    "| **Initial Duration** | 30 days | 60 days |",
    "| **Duration (initial)** | 30 days | 60 days |",
    "- **60 days** initial stay",
    "- **Extendable twice:** +30 days each (total: 120 days)",
    "| **Extension possible?** | ❌ No | ✅ Yes (twice, +30 days each) |",
    "| German, 120-day Bali base | E-VOA + 2 extensions | Maximum tourist stay |",
    "**Second Extension (days 91-120):**",
    "It costs IDR 500,000, grants 60 days initially, and can be extended.",
    "| **Durasi Awal** | 30 hari | 60 hari |",
    "- Masa tinggal awal **60 hari**",
    "- **Dapat diperpanjang dua kali:** +30 hari setiap kali (total: 120 hari)",
    "| **Dapat diperpanjang?** | ❌ Tidak | ✅ Ya (dua kali, +30 hari setiap kali) |",
    "| **Durata (iniziale)** | 30 giorni | 60 giorni |",
    "- **60 giorni** di soggiorno iniziale",
    "- **Estendibile due volte:** +30 giorni ciascuna (totale: 120 giorni)",
    "| **Estensione possibile?** | ❌ No | ✅ Sì (due volte, +30 giorni ciascuna) |",
    "| **Durée initiale** | 30 jours | 60 jours |",
    "- **60 jours** de séjour initial",
    "- **Prolongeable deux fois :** +30 jours chacun (total : 120 jours)",
    "| **Первоначальная длительность** | 30 дней | 60 дней |",
    "- **60 дней** первоначального пребывания",
    "| **Возможно ли продление?** | ❌ Нет | ✅ Да (дважды, по +30 дней каждое) |",
    "| **Максимальное пребывание** | 30 дней | 120 дней (два продления по 30 дней) |",
  ])("rejects the old comparison line: %s", (sample) => {
    expect(restatesOldRule(sample)).toBe(true);
  });
  it.each([
    "| **Initial Duration** | 30 days | 30 days |",
    "| **Initial stay** | 30 days | 30 days (60 with the one extension) |",
    "- **30 days** initial stay",
    "**There is no second extension.** After day 60 you must leave.",
    "   - A separate single-entry visa, initially up to 60 days, extendable under its own rules up to 180 days in total",
    "   - Visa terpisah sekali masuk, awalnya hingga 60 hari, dapat diperpanjang menurut aturannya sendiri hingga total 180 hari",
    "- **Duration:** 30 days on entry, plus the one extension of up to 30 days (60 days maximum)",
    "The 60 days maximum is the initial 30 days plus the one extension.",
    "A 60-day maximum (initial 30 days, one extension of 30).",
    "Periksa paspor Anda dua kali sebelum berangkat.",
    "Check your booking twice before you pay.",
    "Check the address twice before requesting an extension.",
    "Periksa alamat dua kali sebelum mengajukan perpanjangan.",
    "C1 initial stay: 60 days",
    "C1 allows two extensions of 60 days",
    "- **C1:** initial 60 days, extendable twice under its own rules",
    "| **Initial stay** | 30 days | C1: 60 days |",
    "KITAS holders may extend twice before converting to KITAP.",
  ])("spares the corrected comparison line: %s", (sample) => {
    expect(restatesOldRule(sample)).toBe(false);
  });
  it.each([
    ["- **60 days** initial stay", "### C1 tourist visit visa", false],
    [
      "- **Extendable twice:** +60 days each",
      "### C1 tourist visit visa",
      false,
    ],
    ["- **60 days** initial stay", "### E-VOA: Deep Dive", true],
    ["- **Extendable twice:** +30 days each", "### Key Features", true],
    ["- **B1: 60 days** initial stay", "### C1 tourist visit visa", true],
  ])("judges %s under %s by the visa it is about", (line, heading, wrong) => {
    expect(restatesOldRule(line, heading)).toBe(wrong);
  });
  it.each(comparisonFiles)("%s restates the old rule on no line", (file) => {
    const raw = fs.readFileSync(path.join(root, file), "utf8");
    expect(linesRestatingOldRule(raw)).toEqual([]);
  });
  for (const slug of otherSlugs) {
    it(`${slug} translations carry the hash of the current English body`, async () => {
      const { createHash } = await import("node:crypto");
      const bodyOf = (raw: string) =>
        raw.replace(/^---\s*\n([\s\S]*?)\n---\s*\n/, "");
      const english = fs.readFileSync(path.join(root, `${slug}.mdx`), "utf8");
      const digest = createHash("sha256")
        .update(bodyOf(english), "utf8")
        .digest("hex");
      for (const locale of locales.filter(Boolean)) {
        const raw = fs.readFileSync(
          path.join(root, `${slug}${locale}.mdx`),
          "utf8",
        );
        const data = matter(raw).data;
        expect(data.locale).toBe(locale.slice(1));
        expect(data.source_sha256, `${slug}${locale}`).toBe(digest);
        // EN/ID/IT stay aligned; the FR and RU comparison translations are
        // shorter (first five sections only), as before this correction.
        const aligned = !(
          slug === comparison && [".fr", ".ru"].includes(locale)
        );
        const headings = (text: string) => bodyOf(text).match(/^## /gm)?.length;
        if (aligned) expect(headings(raw)).toBe(headings(english));
        else expect(headings(raw)).toBeLessThan(headings(english) ?? 0);
      }
    });
  }
});
