import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

import {
  getNativeScopeView,
  type KBLIDetail,
  type KBLINativeScopeRow,
  type KBLISearchResult,
} from "../kbli.api";
import { lkpmApi, type LKPMAlertSummary } from "../workspace/lkpm.api";
import { lkpmAssignees } from "../../workspace/roster-directory";

const SRC_ROOT = join(__dirname, "..", "..", "..");
const REPO_ROOT = join(SRC_ROOT, "..", "..", "..");

const ALL_22_ATTESTED_CODES = [
  "42914",
  "42915",
  "42991",
  "42994",
  "42995",
  "42998",
  "55106",
  "55201",
  "55203",
  "55209",
  "56101",
  "56102",
  "56303",
  "68111",
  "68210",
  "68292",
  "70201",
  "70209",
  "85510",
  "93116",
  "96220",
  "96230",
] as const;

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([a], [b]) => (a < b ? -1 : a > b ? 1 : 0),
    );
    return `{${entries
      .map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

describe("Swarm Vertical 2 — KBLI & LKPM API Contract Parity", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("KBLI-3A-01 & KBLI-3A-02: sends JSON bodies for uploadReceipt and syncJurnal", async () => {
    apiMocks.post
      .mockResolvedValueOnce({ success: true })
      .mockResolvedValueOnce({
        success: true,
        draft_id: 19,
        realized_total: 250000000,
      });

    await lkpmApi.uploadReceipt(7, "SYNTH-001");
    expect(apiMocks.post).toHaveBeenNthCalledWith(
      1,
      "/api/v1/lkpm/upload-receipt/7",
      { receipt_number: "SYNTH-001" },
    );

    const synced = await lkpmApi.syncJurnal(12, "Q2", 2026);
    expect(apiMocks.post).toHaveBeenNthCalledWith(
      2,
      "/api/v1/lkpm/sync-jurnal/12",
      { quarter: "Q2", year: 2026 },
    );
    expect(synced.draft_id).toBe(19);
  });

  it("KBLI-3C-09: types and unwraps getAlerts() as draft-level LKPMAlertSummary[]", async () => {
    const backendRow: LKPMAlertSummary = {
      draft_id: 42,
      client_id: 101,
      company_name: "Synthetic Holdings",
      quarter: "Q1",
      year: 2026,
      red_alerts: 2,
      total_alerts: 3,
      status: "draft",
    };
    apiMocks.get.mockResolvedValueOnce({
      success: true,
      alerts: [backendRow],
    });

    const alerts = await lkpmApi.getAlerts();
    expect(apiMocks.get).toHaveBeenCalledWith("/api/v1/lkpm/alerts");
    expect(alerts).toEqual([backendRow]);

    apiMocks.get.mockResolvedValueOnce({ success: true });
    expect(await lkpmApi.getAlerts()).toEqual([]);
  });

  it("KBLI-3C-03 & KBLI-3C-04: aligns lkpmAssignees() with backend allowlist and keeps historical fallback option", () => {
    const localParts = lkpmAssignees().map((c) => c.value.split("\x40")[0]);
    expect(localParts).toEqual([
      "krisna",
      "kadek.tax",
      "dewaayu.tax",
      "angel.tax",
    ]);
    expect(localParts).not.toContain("veronika.tax");
    expect(localParts).not.toContain("dea");
    expect(localParts).not.toContain("candra");

    const batchSrc = readFileSync(
      join(SRC_ROOT, "app", "(workspace)", "lkpm", "LkpmBatchClient.tsx"),
      "utf8",
    );
    expect(batchSrc).toContain("!taxConsultants.some(");
    expect(batchSrc).toContain(
      "<option value={item.lkpm_assigned_to} disabled>",
    );
  });

  it("KBLI-3C-01 & KBLI-3C-02: guards nullable validation_summary and reads nested+flat LKPMDraft fields", () => {
    const readyPackPage = readFileSync(
      join(SRC_ROOT, "app", "(workspace)", "lkpm", "[id]", "page.tsx"),
      "utf8",
    );
    expect(readyPackPage).toContain("pack.validation_summary?.is_valid");
    expect(readyPackPage).toContain("pack.validation_summary?.red_count ?? 0");
    expect(readyPackPage).toContain(
      "pack.validation_summary?.yellow_count ?? 0",
    );
    expect(readyPackPage).toContain(
      "pack.validation_summary?.green_count ?? 0",
    );

    const quarterPage = readFileSync(
      join(
        SRC_ROOT,
        "app",
        "portal",
        "(authenticated)",
        "lkpm",
        "[quarter]",
        "page.tsx",
      ),
      "utf8",
    );
    expect(quarterPage).toContain(
      "draft.validation?.alerts ?? draft.validation_alerts ?? []",
    );
    expect(quarterPage).toContain(
      "draft.quarterly_revenue ?? draft.revenue_quarterly ?? 0",
    );
    expect(quarterPage).toContain(
      "draft.annual_revenue ?? draft.revenue_annual ?? 0",
    );
    expect(quarterPage).toContain(
      "draft.narrative_obstacles ?? draft.obstacles ?? null",
    );
    expect(quarterPage).toContain(
      "draft.narrative_plans ?? draft.plans ?? null",
    );
  });

  it("KBLI-3C-05: verifies all 22 attested native-scope codes against canonical dataset", () => {
    const raw = readFileSync(
      join(REPO_ROOT, "data", "source_documents", "KBLI_2025_FINAL_CLEAN.json"),
    );
    const datasetSha = createHash("sha256").update(raw).digest("hex");
    const parsed = JSON.parse(raw.toString("utf8")) as {
      metadata: { version: string };
      data: Array<{
        kode_kbli_2025: string;
        per_skala: Array<{
          scope_index: number;
          scope_uraian: string;
          skala_usaha: string[];
          kategori_risiko: string;
          persyaratan: string[];
          kewajiban: string[];
          kewenangan: string[];
        }>;
      }>;
    };
    const byCode = new Map(parsed.data.map((r) => [r.kode_kbli_2025, r]));

    for (const code of ALL_22_ATTESTED_CODES) {
      const record = byCode.get(code);
      expect(record, `missing ${code} in canonical`).toBeDefined();
      const perSkala = record!.per_skala;
      const rowsSha = createHash("sha256")
        .update(Buffer.from(canonicalJson(perSkala), "utf8"))
        .digest("hex");

      const projected: KBLINativeScopeRow[] = perSkala.map((r, ordinal) => ({
        row_ordinal: ordinal,
        scope_index: r.scope_index,
        scope_uraian: r.scope_uraian,
        scale: r.skala_usaha,
        type: "NIB",
        risk_level: r.kategori_risiko,
        requirements: r.persyaratan.length ? r.persyaratan : ["Requirement"],
        obligations: r.kewajiban.length ? r.kewajiban : ["Obligation"],
        authorities: r.kewenangan.length ? r.kewenangan : ["Authority"],
      }));

      const detail: KBLIDetail = {
        code,
        title: "Test Activity",
        description: "Test Description",
        pma_status: "NOT_VERIFIED",
        pma_max_asing: null,
        pma_cap_special: false,
        pma_cap_verified: false,
        pma_verification_status: "declared_gap",
        pma_official_basis: null,
        pma_source_vintage: null,
        licensing_status: "VERIFIED",
        licensing_content_inherited_from: ["62011"],
        sector: "J",
        risk_profile: "Rendah",
        licenses: [],
        related_requirements: {},
        related_codes: [],
        scope_rows_status: "available",
        scope_rows: projected,
        scope_rows_source: {
          dataset_sha256: datasetSha,
          rows_sha256: rowsSha,
          dataset_version: parsed.metadata.version,
          kbli_vintage: "2025",
          source: "OSS_RBA_resiko_2025",
        },
      };

      const view = getNativeScopeView(detail);
      expect(view?.status, `code ${code} failed attestation`).toBe("available");
      expect(view?.rows).toHaveLength(perSkala.length);
    }

    // Unattested code returns null
    expect(
      getNativeScopeView({
        code: "68112",
        title: "Unattested",
        description: "Desc",
        pma_status: "NOT_VERIFIED",
        pma_max_asing: null,
        pma_cap_special: false,
        pma_cap_verified: false,
        pma_verification_status: "declared_gap",
        pma_official_basis: null,
        pma_source_vintage: null,
        licensing_status: "VERIFIED",
        sector: "L",
        risk_profile: "Rendah",
        licenses: [],
        related_requirements: {},
        related_codes: [],
      }),
    ).toBeNull();
  });

  it("KBLI-3C-08 & KBLI-3A-04/3D-03: keeps KBLISearchResult flat with bali_* fields and guards PrimeMap3D search-kbli", () => {
    const flatResult: KBLISearchResult = {
      code: "86995",
      title: "Health Activity",
      description: "Desc",
      score: 0.92,
      risk_category: "Tinggi",
      pma_status: "TERBUKA",
      pma_max_asing: 100,
      pma_cap_special: false,
      pma_cap_verified: true,
      pma_verification_status: "located",
      pma_official_basis: "Perpres 49/2021",
      pma_source_vintage: "2021-05-25",
      bali_status: "blocked",
      bali_blocked: true,
      bali_needs_review: false,
      bali_reason: "Provincial closure",
      bali_closure_url: "https://peraturan.bpk.go.id/example",
      bali_closure_scope: "provincial",
      bali_confidence: "verified",
    };
    expect(flatResult.bali_blocked).toBe(true);
    expect("metadata" in flatResult).toBe(false);

    const primeMapSrc = readFileSync(
      join(SRC_ROOT, "components", "maps", "PrimeMap3D.tsx"),
      "utf8",
    );
    expect(primeMapSrc).toContain("if (kbliQuery.length < 2)");
    expect(primeMapSrc).toContain("if (!res.ok)");
    expect(primeMapSrc).toContain('if (data.status === "error")');
  });
});
