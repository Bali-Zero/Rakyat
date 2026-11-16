import fs from "fs";
import path from "path";
import { describe, it, expect } from "vitest";
import { ENGLISH_TITLES } from "./kbli-english";
import { ENGLISH_TITLES_GENERATED } from "./kbli-english-generated";
import { LEGACY_ENGLISH_TITLES } from "./kbli-legacy-english";
import type { KBLIRawDataFile } from "./kbli-types";

const MOUTH_ROOT = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../..",
);
const DATA_PATH = path.join(MOUTH_ROOT, "data/KBLI_2025_FINAL_CLEAN.json");

function readMouthFile(relPath: string): string {
  return fs.readFileSync(path.join(MOUTH_ROOT, relPath), "utf8");
}

describe("ENGLISH_TITLES", () => {
  it("is a non-empty Record", () => {
    expect(Object.keys(ENGLISH_TITLES).length).toBeGreaterThan(0);
  });

  it("maps KBLI codes to English strings", () => {
    expect(typeof ENGLISH_TITLES["55101"]).toBe("string");
    expect(ENGLISH_TITLES["55101"]).toBe("Five-Star Hotel");
  });

  it("contains accommodation sector codes", () => {
    expect(ENGLISH_TITLES["55201"]).toBe("Homestay");
    expect(ENGLISH_TITLES["55203"]).toBe("Villa Rental");
  });

  it("contains food and beverage codes", () => {
    expect(ENGLISH_TITLES["56101"]).toBe("Restaurant");
  });

  it("has string keys that look like KBLI codes (digits only)", () => {
    for (const key of Object.keys(ENGLISH_TITLES)) {
      expect(key).toMatch(/^\d+$/);
    }
  });

  it("covers all 1,559 canonical KBLI 2025 codes across ENGLISH_TITLES + ENGLISH_TITLES_GENERATED", () => {
    const raw = JSON.parse(
      fs.readFileSync(DATA_PATH, "utf8"),
    ) as KBLIRawDataFile;
    expect(raw.data.length).toBe(1559);

    const missing: string[] = [];
    for (const row of raw.data) {
      const en = (
        ENGLISH_TITLES[row.kode_kbli_2025] ||
        ENGLISH_TITLES_GENERATED[row.kode_kbli_2025] ||
        ""
      ).trim();
      if (!en) {
        missing.push(row.kode_kbli_2025);
      }
    }
    expect(missing).toEqual([]);
  });

  it("provides 5-digit legacy KBLI 2020 English titles for transition crosswalks", () => {
    const keys = Object.keys(LEGACY_ENGLISH_TITLES);
    expect(keys.length).toBeGreaterThanOrEqual(123);
    for (const key of keys) {
      expect(key).toMatch(/^\d{5}$/);
      expect(LEGACY_ENGLISH_TITLES[key].trim().length).toBeGreaterThan(0);
    }
  });
});

describe("KBLI Web EN view purity guards", () => {
  const FORBIDDEN_ID_UI_STRINGS = [
    "Tidak yakin kode",
    "Coba KBLI Decoder",
    "Mau setup PT PMA",
    "Coba KBLI Builder",
    "Temukan Kode Klasifikasi",
    "Susun Struktur Kode",
    "Mulai Konsultasi via WhatsApp",
    "Mengapa kode yang tepat penting",
    "Cara kerja",
    "Buka KBLI Navigator",
    "Daftar Negatif Investasi",
    "Perubahan DNI 2024",
    "1.563",
  ];

  const EN_FUNNEL_FILES = [
    "src/app/kbli/page.tsx",
    "src/app/kbli/decoder/page.tsx",
    "src/app/kbli/decoder/_components/KBLIDecoderCTA.tsx",
    "src/app/kbli/builder/page.tsx",
    "src/app/kbli/builder/_components/KBLIBuilderCTA.tsx",
  ];

  it("keeps /kbli, /kbli/decoder, and /kbli/builder free of residual Indonesian UI copy and stale DNI/1.563 claims", () => {
    const hits: string[] = [];
    for (const rel of EN_FUNNEL_FILES) {
      const src = readMouthFile(rel);
      for (const phrase of FORBIDDEN_ID_UI_STRINGS) {
        if (src.includes(phrase)) {
          hits.push(`${rel}: "${phrase}"`);
        }
      }
    }
    expect(hits).toEqual([]);
  });

  it("presents English CTAs on /kbli matching /kbli/decoder and /kbli/builder", () => {
    const landing = readMouthFile("src/app/kbli/page.tsx");
    expect(landing).toContain("Not sure which code fits your business?");
    expect(landing).toContain("Try KBLI Decoder →");
    expect(landing).toContain("Setting up a PT PMA?");
    expect(landing).toContain("Try KBLI Builder →");
  });

  it("never renders raw Indonesian kbli.description unlabelled on /kbli/[code]", () => {
    const codePage = readMouthFile("src/app/kbli/[code]/page.tsx");
    const descMatches = [...codePage.matchAll(/\{kbli\.description\}/g)];
    expect(descMatches.length).toBeGreaterThan(0);

    for (const match of descMatches) {
      const idx = match.index ?? 0;
      const windowBefore = codePage.slice(Math.max(0, idx - 450), idx);
      expect(windowBefore).toContain(
        "Official BPS Scope Description (Bahasa Indonesia)",
      );
      expect(windowBefore).toContain('lang="id"');
    }
  });

  it('labels Indonesian statutory titles with lang="id" across code, sector, and panel views', () => {
    const codePage = readMouthFile("src/app/kbli/[code]/page.tsx");
    expect(codePage).toContain("Official title (Bahasa Indonesia):");
    expect(codePage).toContain('<span lang="id">{kbli.titleId}</span>');
    expect(codePage).not.toMatch(
      /opacity-\d+[^>]*>\s*Official title \(Bahasa Indonesia\):/,
    );

    const panelDetail = readMouthFile(
      "src/components/kbli/KBLIPanelCodeDetail.tsx",
    );
    expect(panelDetail).toContain('<span lang="id">{detail.titleId}</span>');

    const sectorPage = readMouthFile("src/app/kbli/sectors/[id]/page.tsx");
    expect(sectorPage).toContain("Official BPS section (Bahasa Indonesia):");
    expect(sectorPage).toContain('<span lang="id">{meta.nameId}</span>');
    expect(sectorPage).not.toMatch(
      /opacity-\d+[^>]*>\s*Official BPS section \(Bahasa Indonesia\):/,
    );

    const sectorPanel = readMouthFile(
      "src/app/kbli/@panel/(.)sectors/[id]/page.tsx",
    );
    expect(sectorPanel).toContain('<span lang="id">{meta.nameId}</span>');
  });
});
