import { ApiClientBase } from "./client";
import { knownPmaRawStatus } from "../kbli-provenance";

export interface KBLILicense {
  type: string;
  scale: string[];
  risk_level: string;
  sla: string;
  requirements: string[];
}

export interface KBLISource {
  chunk_id: string;
  content: string;
  metadata: {
    kode_kbli?: string;
    judul?: string;
    source?: string;
  };
  score: number;
}

export type KBLIRelatedRequirements = Record<string, string[]>;

export interface KBLIPmaDisclosure {
  pma_status: string;
  pma_max_asing: number | string | null;
  pma_cap_special: boolean;
  pma_cap_verified: boolean;
  pma_verification_status: string;
  pma_official_basis: string | null;
  pma_source_vintage: string | null;
}

export type ApiPmaPublicStatus = "open" | "restricted" | "closed" | "unknown";

export interface ApiPmaPresentation {
  status: ApiPmaPublicStatus;
  statusLabel: string;
  ownershipLabel: string;
  compactLabel: string;
  capVerified: boolean;
}

export function isApiPmaVerdictVerified(record: KBLIPmaDisclosure): boolean {
  const basis = record.pma_official_basis;
  const vintage = record.pma_source_vintage;
  return (
    record.pma_verification_status === "located" &&
    !!knownPmaRawStatus(record.pma_status) &&
    typeof basis === "string" &&
    !!basis.trim() &&
    typeof vintage === "string" &&
    !!vintage.trim()
  );
}

/**
 * Exact public API presentation gate. A located whole-code status does not
 * verify its ownership ceiling: the cap needs its own affirmative flag and a
 * finite numeric value (or the exact marked-special tuple).
 */
export function apiPmaPresentation(
  record: KBLIPmaDisclosure,
): ApiPmaPresentation {
  const rawStatus = knownPmaRawStatus(record.pma_status);
  if (!isApiPmaVerdictVerified(record) || !rawStatus) {
    return {
      status: "unknown",
      statusLabel: "PMA not verified",
      ownershipLabel: "PMA Not Verified",
      compactLabel: "PMA not verified",
      capVerified: false,
    };
  }

  const numericCap =
    typeof record.pma_max_asing === "number" &&
    Number.isFinite(record.pma_max_asing);
  const specialCap =
    record.pma_max_asing === "special" && record.pma_cap_special === true;
  const capVerified =
    record.pma_cap_verified === true && (numericCap || specialCap);
  const qualifier = capVerified ? "" : " · ownership cap not verified";

  if (rawStatus === "TERBUKA") {
    return {
      status: "open",
      statusLabel: rawStatus + qualifier,
      ownershipLabel: "Open to Foreign Investment" + qualifier,
      compactLabel: "Open to Foreigners" + qualifier,
      capVerified,
    };
  }
  if (rawStatus === "TERBATAS") {
    return {
      status: "restricted",
      statusLabel: rawStatus + qualifier,
      ownershipLabel: "Restricted - Conditions Apply" + qualifier,
      compactLabel: "Restricted" + qualifier,
      capVerified,
    };
  }
  return {
    status: "closed",
    statusLabel: rawStatus + qualifier,
    ownershipLabel: "Closed to Foreign Investment" + qualifier,
    compactLabel: "Closed to Foreigners" + qualifier,
    capVerified,
  };
}

export function apiPmaStatusLabel(record: KBLIPmaDisclosure): string {
  return apiPmaPresentation(record).statusLabel;
}

export interface KBLINativeScopeRow {
  row_ordinal: number;
  scope_index: number;
  scope_uraian: string;
  scale: string[];
  type: string;
  risk_level: string;
  requirements: string[];
  obligations: string[];
  authorities: string[];
}

interface KBLINativeScopeSource {
  dataset_sha256: string;
  rows_sha256: string;
  dataset_version: string;
  kbli_vintage: "2025";
  source: string;
}

export interface KBLINativeScopeView {
  status: "available" | "verified_empty" | "unavailable";
  rows: KBLINativeScopeRow[];
}

export interface KBLIDetail extends KBLIPmaDisclosure {
  licensing_note?: string | null;
  code: string;
  title: string;
  description: string;
  licensing_status: string;
  sector: string;
  risk_profile: string;
  licenses: KBLILicense[];
  scope_rows?: KBLINativeScopeRow[] | null;
  scope_rows_status?: KBLINativeScopeView["status"];
  scope_rows_source?: KBLINativeScopeSource | null;
  related_requirements: KBLIRelatedRequirements;
  related_codes: string[];
  intel?: {
    whatItMeans: string;
    whatYouNeed: string;
    whatChanged: string;
    baliContext: string;
    zantaraOpener: string;
    youllAlsoNeed?: string;
  };
}

function isDisplayableScopeRow(
  row: KBLINativeScopeRow,
  ordinal: number,
): boolean {
  return Boolean(
    row &&
    row.row_ordinal === ordinal &&
    Number.isInteger(row.scope_index) &&
    row.scope_index >= 0 &&
    [row.scope_uraian, row.type, row.risk_level].every(
      (text) => typeof text === "string" && text.trim(),
    ) &&
    [row.scale, row.requirements, row.obligations, row.authorities].every(
      (items) =>
        Array.isArray(items) &&
        items.every((text) => typeof text === "string" && text.trim()),
    ) &&
    row.scale.length > 0,
  );
}

// Source-pinned row identities for the explicitly verified codes. Each tuple
// binds scope, scale and risk; counts and risk tiers may vary within a scope.
type NativeScopeIdentity = [number, string, string, string];
const NATIVE_SCOPE_ATTESTATIONS: Record<
  string,
  { rowsSha256: string; rows: NativeScopeIdentity[] }
> = {
  "55106": {
    rowsSha256:
      "eda9f92d5f0fc28aa612f29a9e51967dcf1b08e6104ba99c65297591ba3fc14c", // pragma: allowlist secret
    rows: [
      [0, "Dengan Luas bangunan > 6.000 M²", "Kecil", "Menengah Tinggi"],
      [0, "Dengan Luas bangunan > 6.000 M²", "Menengah", "Menengah Tinggi"],
      [0, "Dengan Luas bangunan > 6.000 M²", "Besar", "Menengah Tinggi"],
      [1, "Dengan luas bangunan ≤ 6.000 M²", "Kecil", "Menengah Rendah"],
      [1, "Dengan luas bangunan ≤ 6.000 M²", "Menengah", "Menengah Rendah"],
      [1, "Dengan luas bangunan ≤ 6.000 M²", "Besar", "Menengah Rendah"],
    ],
  },
  "55201": {
    rowsSha256:
      "f91730f158d9349962c9bb5be1654b55365cd0e04cdf9d7381a995ebdccc484c", // pragma: allowlist secret
    rows: [
      [0, "Seluruh", "Mikro", "Menengah Rendah"],
      [0, "Seluruh", "Kecil", "Menengah Rendah"],
      [0, "Seluruh", "Menengah", "Menengah Rendah"],
    ],
  },
  "55203": {
    rowsSha256:
      "26d0209c36cd531dad9e58a65564956429e6cdd56e3a970a55aea9b05f45601c", // pragma: allowlist secret
    rows: [
      [0, "Seluruh", "Mikro", "Menengah Rendah"],
      [0, "Seluruh", "Kecil", "Menengah Rendah"],
      [0, "Seluruh", "Menengah", "Menengah Rendah"],
    ],
  },
  "56101": {
    rowsSha256:
      "fe7c191cbb2393ba38e8a4d5f7ca613ab45cae81f6e3fadc5e1b0999f2150449", // pragma: allowlist secret
    rows: [
      [0, "Restoran", "Mikro", "Menengah Rendah"],
      [0, "Restoran", "Kecil", "Menengah Tinggi"],
      [0, "Restoran", "Menengah", "Menengah Tinggi"],
      [0, "Restoran", "Besar", "Menengah Tinggi"],
      [1, "Rumah / Warung Makan", "Mikro", "Menengah Rendah"],
      [1, "Rumah / Warung Makan", "Kecil", "Menengah Rendah"],
    ],
  },
  "56303": {
    rowsSha256:
      "a8d9ed6b61d000e481e0ae56f6cc5fc34f3359f788291d338ebfde9bf01fa967", // pragma: allowlist secret
    rows: [
      [0, "Seluruh", "Mikro", "Menengah Rendah"],
      [0, "Seluruh", "Kecil", "Menengah Rendah"],
      [0, "Seluruh", "Menengah", "Menengah Rendah"],
      [0, "Seluruh", "Besar", "Menengah Rendah"],
    ],
  },
  "55209": {
    rowsSha256:
      "7e7304ecbe0531f9a3522494475c5818a0f82d15954f8e8371a50f150ed32909", // pragma: allowlist secret
    rows: [
      [0, "Seluruh", "Mikro", "Menengah Rendah"],
      [0, "Seluruh", "Kecil", "Menengah Rendah"],
      [0, "Seluruh", "Menengah", "Menengah Rendah"],
    ],
  },
  "56102": {
    rowsSha256:
      "a1e3eee1d40a6a769cc9f8e9ad668e8f4b1ecbb85c34df58a92993d3828c5e94", // pragma: allowlist secret
    rows: [
      [0, "Kedai Makan", "Mikro", "Menengah Rendah"],
      [0, "Kedai Makan", "Kecil", "Menengah Rendah"],
      [
        1,
        "Penyediaan Makanan Keliling / Tempat Tidak Tetap",
        "Mikro",
        "Menengah Rendah",
      ],
      [
        1,
        "Penyediaan Makanan Keliling / Tempat Tidak Tetap",
        "Kecil",
        "Menengah Rendah",
      ],
      [
        2,
        "Restoran dan Penyediaan Makanan Keliling Lainnya",
        "Mikro",
        "Menengah Rendah",
      ],
      [
        2,
        "Restoran dan Penyediaan Makanan Keliling Lainnya",
        "Kecil",
        "Menengah Rendah",
      ],
    ],
  },
};

function isCompleteNativeCoverage(
  rows: KBLINativeScopeRow[],
  expectedRows: NativeScopeIdentity[],
): boolean {
  return (
    rows.length === expectedRows.length &&
    rows.every(
      (row, ordinal) =>
        isDisplayableScopeRow(row, ordinal) &&
        row.scale.length === 1 &&
        JSON.stringify([
          row.scope_index,
          row.scope_uraian,
          row.scale[0],
          row.risk_level,
        ]) === JSON.stringify(expectedRows[ordinal]),
    )
  );
}

export function getNativeScopeView(
  data: KBLIDetail,
): KBLINativeScopeView | null {
  const attestation = NATIVE_SCOPE_ATTESTATIONS[data.code];
  if (!attestation) return null;
  const source = data.scope_rows_source;
  const rows = data.scope_rows;
  if (
    source?.kbli_vintage === "2025" &&
    source.source === "OSS_RBA_resiko_2025" &&
    typeof source.dataset_version === "string" &&
    source.dataset_version.trim() &&
    /^[a-f0-9]{64}$/.test(source.dataset_sha256) &&
    source.rows_sha256 === attestation.rowsSha256 &&
    Array.isArray(rows)
  ) {
    if (
      data.scope_rows_status === "available" &&
      isCompleteNativeCoverage(rows, attestation.rows)
    )
      return { status: "available", rows };
  }
  return { status: "unavailable", rows: [] };
}

export interface KBLISearchResult extends KBLIPmaDisclosure {
  code: string;
  title: string;
  description: string;
  score: number;
  risk_category: string;
}

export class KBLIApi extends ApiClientBase {
  constructor(baseUrl: string) {
    super(baseUrl);
  }

  async search(query: string): Promise<KBLISearchResult[]> {
    return this.request<KBLISearchResult[]>(
      `/api/v1/kbli-notebook/search?query=${encodeURIComponent(query)}`,
    );
  }

  async inspect(code: string): Promise<KBLIDetail> {
    return this.request<KBLIDetail>(`/api/v1/kbli-notebook/inspect/${code}`);
  }

  async chat(
    query: string,
    sessionId?: string,
  ): Promise<{
    answer: string;
    detected_kbli: string[];
    results: KBLISearchResult[];
    sources: KBLISource[];
    suggested_queries: string[];
  }> {
    return this.post<{
      answer: string;
      detected_kbli: string[];
      results: KBLISearchResult[];
      sources: KBLISource[];
      suggested_queries: string[];
    }>("/api/v1/kbli-notebook/chat", {
      query,
      session_id: sessionId,
    });
  }
}

export function resolveKbliBaseUrl(
  isBrowser: boolean = typeof window !== "undefined",
  envBaseUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL,
): string {
  if (isBrowser) {
    return "";
  }
  return envBaseUrl?.trim() || "http://localhost:8080";
}

// Export a singleton instance routed through same-origin proxy in the browser
const baseUrl = resolveKbliBaseUrl();
export const kbliApi = new KBLIApi(baseUrl);
