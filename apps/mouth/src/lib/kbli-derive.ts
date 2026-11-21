/**
 * kbli-derive.ts — shared derivation helpers that bring the web card to parity with the native
 * KBLI Navigator app (Swift), 2026-06-28.
 *
 * Two PP 28/2025-grounded derivations the raw dataset does NOT carry directly, ported verbatim
 * from the Swift app so the web shows the same correct values:
 *
 *  1. licenseForRisk — when a scale's `perizinan` (licenseType) is empty (true on the ~1338
 *     array-form records where the legacy scalar was never filled), DERIVE the license from the
 *     risk tier. PP 28/2025 Pasal 124(4): "Tingkat Risiko menentukan jenis Perizinan Berusaha".
 *     The web previously fell back to a flat "NIB", which UNDERSTATED the requirement on the 937
 *     high-risk codes (Tinggi needs NIB + Izin, not bare NIB). Mirrors
 *     KBLIRegistryView.licenseForRisk.
 *
 *  2. formatTimeframe — render the jangka_waktu cleanly: "Otomatis" → "Instant", a bare day count
 *     ("3", "14") → "3 working days", an already-unit'd "5 Hari" → "5 working days" (strip the
 *     double unit), and the special-regime labels we just added ("Sesuai tahapan IUP (ESDM)",
 *     "Sesuai ketentuan OJK/BI") pass through verbatim. Mirrors PP28ScalePanel.formatJangkaWaktu.
 *
 * Keep these pure and dependency-free — both kbli-data.ts (client) and kbli-data.server.ts import
 * them, so the derivation lives in ONE place (the Swift bug was that each render site re-derived
 * differently; here we derive once in the transformer).
 */

import { isSourceTruncated } from "./kbli-obligation-truncation";

/** License type derived from the risk tier when the explicit `perizinan` is empty (Pasal 124(4)). */
export function licenseForRisk(risk: string | null | undefined): string {
  const r = (risk || "").trim();
  // Tinggi → NIB + Izin (a verified permit on top of NIB). Menengah Tinggi / Menengah Rendah →
  // NIB + Sertifikat Standar. Rendah → NIB alone (instant). Unknown → NIB (safe minimum).
  if (r === "Tinggi") return "NIB + Izin";
  if (r === "Menengah Tinggi" || r === "Menengah Rendah")
    return "NIB + Sertifikat Standar";
  if (r === "Rendah") return "NIB";
  return "NIB";
}

/**
 * A licence NAME that stops mid-sentence is unusable, and is treated exactly
 * like an absent one so the risk-derived fallback below fires.
 *
 * Measured 2026-08-05: canonical carries **`"NIB dan"` on 21 codes** (and the
 * knowledge graph on 12, plus `"Sertifikasi Cara Budi Daya Ternak Yang"` on 2).
 * `"NIB dan"` — literally *"NIB and"* — was rendered as the licence type at TEN
 * sites, including `KBLIStructuredData` (schema.org JSON-LD that reaches Google)
 * and `kbli-meta` (the page `<title>`).
 *
 * WHY THE FALLBACK AND NOT THE `[cut off]` LABEL used for obligation TEXT: two
 * of those ten sites are metadata, where a UI annotation would be actively
 * wrong — you do not put "[cut off in the official source]" inside a page title
 * or structured data. `licenseForRisk` is not a guess at what the truncated
 * string said: it applies the documented OSS-RBA rule risk-tier → licence-tier,
 * which is already this product's behaviour for the ~1,338 codes whose
 * `perizinan` is empty. Treating an unusable name as absent reuses that
 * established path rather than inventing a second one.
 */
function usableLicenceName(value: string | null | undefined): string {
  const trimmed = (value || "").trim();
  // Innocence: a COMPLETE name that merely contains `dan` — "NIB dan
  // Sertifikat Standar" — ends on `Standar` and is kept, pinned by test.
  return trimmed && !isSourceTruncated(trimmed) ? trimmed : "";
}

/**
 * Resolve the license type for a scale: the explicit value if present, else derived from risk.
 *
 * IMPORTANT: in the real dataset `perizinan` is an ARRAY on ~99.5% of scales (the legacy
 * `string` type in kbli-types was a lie — it is a `string[]`, frequently empty `[]`), and a
 * bare `string` on the ~20 legacy records. Handle BOTH: join the distinct non-empty array
 * entries; fall back to the scalar; and when nothing is explicit (empty array / empty string /
 * a name truncated mid-sentence), derive from the risk tier. Mirrors the Swift app's permitText
 * (scalar → array → derive).
 */
export function resolveLicenseType(
  perizinan: string | string[] | null | undefined,
  risk: string | null | undefined,
): string {
  if (Array.isArray(perizinan)) {
    const distinct = Array.from(
      new Set(perizinan.map(usableLicenceName).filter(Boolean)),
    );
    if (distinct.length > 0) return distinct.join(" · ");
    return licenseForRisk(risk);
  }
  const p = usableLicenceName(perizinan);
  if (p) return p;
  return licenseForRisk(risk);
}

const _BARE_DAYS = /^(\d{1,3})$/;
const _N_HARI = /^(\d{1,3})\s*[Hh]ari(?:\s*[Kk]erja)?$/;

/**
 * Format a raw jangka_waktu for display. Returns null when there is genuinely nothing to show
 * (empty / lone dash) — callers decide the placeholder. Never invents a value.
 */
export function formatTimeframe(raw: string | null | undefined): string | null {
  const s = (raw || "").trim();
  if (!s || s === "-" || s === "—") return null;
  if (s.toLowerCase() === "otomatis") return "Instant";
  const bare = _BARE_DAYS.exec(s);
  if (bare) return `${bare[1]} working days`;
  const hari = _N_HARI.exec(s);
  if (hari) return `${hari[1]} working days`;
  // special-regime labels ("Sesuai tahapan IUP (ESDM)", "Sesuai ketentuan OJK/BI") and any other
  // descriptive value pass through verbatim — they are already human-readable.
  return s;
}

/**
 * English label for a raw kategori_risiko value ("Tinggi", "Menengah Rendah",
 * ...). Same semantics as RiskBadge's parseRisk — check the compound
 * "menengah" forms BEFORE the bare ones. Returns null for unknown values:
 * a surface omitting the risk is honest, a surface inventing one is not.
 */
export function riskLabelEn(category?: string | null): string | null {
  if (!category) return null;
  const lower = category.toLowerCase();
  const menengah = lower.includes("menengah");
  if (menengah && lower.includes("tinggi")) return "Medium-High";
  if (menengah && lower.includes("rendah")) return "Medium-Low";
  if (lower.includes("tinggi")) return "High";
  if (lower.includes("rendah")) return "Low";
  return null;
}

/**
 * Numeric rank for a raw kategori_risiko tier, matching macOS Theme.RiskTier.rank:
 * Rendah (1) < Menengah Rendah (2) < Menengah Tinggi (3) < Tinggi (4).
 * Compound "menengah" forms are checked BEFORE bare "tinggi"/"rendah".
 * Unknown/empty returns 0 so callers never fabricate a tier.
 */
export function riskRank(category?: string | null): number {
  if (!category) return 0;
  const lower = category.toLowerCase();
  const menengah = lower.includes("menengah");
  if (menengah && lower.includes("tinggi")) return 3;
  if (menengah && lower.includes("rendah")) return 2;
  if (lower.includes("tinggi")) return 4;
  if (lower.includes("rendah")) return 1;
  return 0;
}

/**
 * English label for a raw skala_usaha value ("Mikro" → "Micro", "Kecil" → "Small",
 * "Menengah" → "Medium", "Besar" → "Large"), matching macOS PP28ScalePanel.scaleLabel.
 * Returns null for unknown/empty values so callers never guess.
 */
export function scaleLabelEn(scale?: string | null): string | null {
  if (!scale) return null;
  switch (scale.trim().toLowerCase()) {
    case "mikro":
      return "Micro";
    case "kecil":
      return "Small";
    case "menengah":
      return "Medium";
    case "besar":
      return "Large";
    default:
      return null;
  }
}

/** Format an array of raw Indonesian business scales in English, preserving unknown tokens verbatim. */
export function formatScalesEn(
  scales: readonly string[],
  separator = " + ",
): string {
  return scales.map((s) => scaleLabelEn(s) ?? s).join(separator);
}

/**
 * Summarize risk across all served licensing rows without understating multi-tier codes.
 *
 * 526 canonical codes carry multiple risk tiers across scales/scopes (and on 501 of them,
 * `licensing[0]` — typically Mikro — is lower than Besar, which is the mandatory PT PMA scale).
 * Mirrors macOS KBLIRegistryView's `lo → hi` range ("Low → High" / "Medium-Low → Medium-High"):
 * - `"en"` mode (LicensingQuickFacts): `"Medium-Low"` or `"Medium-Low → Medium-High"`
 * - `"en-with-id"` mode (LicensingSection KeyFacts): `"Medium-Low (Menengah Rendah)"` or
 *   `"Medium-Low → Medium-High (Menengah Rendah → Menengah Tinggi)"`
 */
export function summarizeRiskRange(
  entries: ReadonlyArray<{ riskCategory: string }>,
  mode: "en" | "en-with-id" = "en",
): string | null {
  const valid = entries
    .map((e) => (e.riskCategory || "").trim())
    .filter(Boolean);
  if (valid.length === 0) return null;

  const ranked = valid
    .map((raw) => ({ raw, rank: riskRank(raw) }))
    .filter((x) => x.rank > 0)
    .sort((a, b) => a.rank - b.rank);

  if (ranked.length === 0) {
    const distinct = Array.from(new Set(valid));
    return distinct.length === 1
      ? distinct[0]
      : `${distinct[0]} → ${distinct[distinct.length - 1]}`;
  }

  const lo = ranked[0].raw;
  const hi = ranked[ranked.length - 1].raw;
  const loEn = riskLabelEn(lo) ?? lo;
  const hiEn = riskLabelEn(hi) ?? hi;

  if (ranked[0].rank === ranked[ranked.length - 1].rank) {
    return mode === "en-with-id" && loEn !== lo ? `${loEn} (${lo})` : loEn;
  }

  if (mode === "en-with-id") {
    return `${loEn} → ${hiEn} (${lo} → ${hi})`;
  }
  return `${loEn} → ${hiEn}`;
}

function licenseRank(license: string): number {
  const lower = license.toLowerCase();
  if (lower.includes("izin")) return 3;
  if (lower.includes("sertifikat standar")) return 2;
  if (lower === "nib") return 1;
  return 2;
}

/**
 * Summarize licenseType across all served licensing rows: single string when uniform across
 * scales (e.g. 56101: "NIB + Sertifikat Standar"), or `lo → hi` ordered by licence tier when
 * larger scales require a stricter permit than Mikro (e.g. 01115: "NIB → NIB + Sertifikat Standar").
 */
export function summarizeLicenseRange(
  entries: ReadonlyArray<{ licenseType: string }>,
): string | null {
  const valid = entries
    .map((e) => (e.licenseType || "").trim())
    .filter(Boolean);
  if (valid.length === 0) return null;

  const distinct = Array.from(new Set(valid));
  if (distinct.length === 1) return distinct[0];

  const sorted = [...distinct].sort((a, b) => licenseRank(a) - licenseRank(b));
  return `${sorted[0]} → ${sorted[sorted.length - 1]}`;
}

/**
 * Summarize processing timeframe across all served licensing rows: single formatted string when
 * uniform across scales, or `first → last` when Mikro is Instant ("Otomatis") and larger scales
 * carry working-day SLAs (so a multi-scale code never claims universal "Instant" issuance).
 */
export function summarizeTimeframeRange(
  entries: ReadonlyArray<{ timeframe: string }>,
): string | null {
  const formatted = Array.from(
    new Set(
      entries
        .map((e) => formatTimeframe(e.timeframe))
        .filter((v): v is string => v !== null),
    ),
  );
  if (formatted.length === 0) return null;
  if (formatted.length === 1) return formatted[0];
  return `${formatted[0]} → ${formatted[formatted.length - 1]}`;
}
