import { resolve } from "node:path";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getNativeScopeView,
  kbliApi,
  type KBLIDetail,
} from "@/lib/api/kbli.api";
import KBLIInspector, { buildInspectorCopyLine } from "./KBLIInspector";
import ComparisonModal from "./ComparisonModal";

const codes = ["55106", "55203"];
const legacy: KBLIDetail = {
  code: "55106",
  title: "Hotel fixture",
  sector: "I",
  description: "Synthetic activity",
  licenses: [
    {
      type: "Legacy-only licence",
      scale: ["All"],
      risk_level: "Tinggi",
      sla: "",
      requirements: [],
    },
  ],
  related_codes: [],
  related_requirements: {},
  risk_profile: "Tinggi",
  licensing_status: "REGULATED",
  pma_status: "NOT_VERIFIED",
  pma_max_asing: null,
  pma_cap_special: false,
  pma_cap_verified: false,
  pma_verification_status: "declared_gap",
  pma_official_basis: null,
  pma_source_vintage: null,
};
const verified: KBLIDetail = {
  ...legacy,
  scope_rows_status: "available",
  scope_rows_source: {
    source: "OSS_RBA_resiko_2025",
    kbli_vintage: "2025",
    dataset_version: "fixture",
    dataset_sha256: "a".repeat(64),
    rows_sha256:
      "eda9f92d5f0fc28aa612f29a9e51967dcf1b08e6104ba99c65297591ba3fc14c",
  },
  scope_rows: [
    "Dengan Luas bangunan > 6.000 M²",
    "Dengan luas bangunan ≤ 6.000 M²",
  ].flatMap((scope, i) =>
    ["Kecil", "Menengah", "Besar"].map((scale, j) => ({
      scope_index: i,
      row_ordinal: i * 3 + j,
      scope_uraian: scope,
      scale: [scale],
      risk_level: i === 0 ? "Menengah Tinggi" : "Menengah Rendah",
      type: `Native ${i} licence`,
      requirements: [`<strong>Requirement ${i}-${j}</strong>`],
      obligations: [`Obligation ${i}-${j}`],
      authorities: [],
    })),
  ),
};
afterEach(() => vi.restoreAllMocks());

describe("55106 native scope presentation", () => {
  it("keeps each scope, scale, risk and term together in the inspector and in copied summaries", () => {
    render(<KBLIInspector data={verified} isLoading={false} />);
    const region = screen.getByRole("region", { name: "Licensing by scope" });
    const rendered = region.querySelectorAll("details");
    expect(rendered.length).toBe(6);
    const copied = buildInspectorCopyLine(verified);
    for (const row of verified.scope_rows!) {
      const body = rendered[row.row_ordinal];
      const summary = [row.scope_uraian, row.scale[0], row.risk_level].join(
        " · ",
      );
      expect(body.querySelector("summary")?.textContent).toBe(summary);
      expect(copied).toContain(summary + " · " + row.type);
      const headings = within(body).getAllByRole("heading", {
        level: 4,
        hidden: true,
      });
      expect(headings.map((h) => h.textContent)).toEqual([
        "Requirements",
        "Obligations",
      ]);
      expect(headings[0].parentElement).toHaveTextContent(
        `Requirement ${row.scope_index}-${row.row_ordinal % 3}`,
      );
      expect(headings[1].parentElement).toHaveTextContent(row.obligations[0]);
    }
    expect(region.querySelector("strong")?.textContent).toBe("Requirement 0-0");
    expect(screen.queryByText("Legacy-only licence")).not.toBeInTheDocument();
    expect(copied).not.toContain("Legacy-only licence");
  });

  it.each([
    ["old backend", legacy],
    [
      "unavailable",
      { ...verified, scope_rows_status: "unavailable", scope_rows: null },
    ],
    ["missing source", { ...verified, scope_rows_source: null }],
    [
      "malformed row",
      {
        ...verified,
        scope_rows: [{ ...verified.scope_rows![0], requirements: null }],
      },
    ],
    [
      "incomplete source response",
      { ...verified, scope_rows: verified.scope_rows!.slice(0, 5) },
    ],
    [
      "repeated scale within one scope",
      {
        ...verified,
        scope_rows: verified.scope_rows!.map((r, i) =>
          i === 5 ? { ...r, scale: ["Menengah"] } : r,
        ),
      },
    ],
    [
      "conflicting scope labels",
      {
        ...verified,
        scope_rows: verified.scope_rows!.map((r, i) =>
          i === 5 ? { ...r, scope_uraian: "Other activity" } : r,
        ),
      },
    ],
    ["unattested empty", { ...verified, scope_rows: [] }],
  ] as const)("reports an unverified scope on %s", (_label, data) => {
    const record = data as KBLIDetail;
    expect(getNativeScopeView(record)?.status).toBe("unavailable");
    expect(buildInspectorCopyLine(record)).toContain(
      "Scope coverage not verified",
    );
    expect(buildInspectorCopyLine(record)).not.toContain("Legacy-only licence");
  });

  it("preserves other codes and rejects an empty claim for a known nonempty source", () => {
    expect(getNativeScopeView({ ...verified, code: "56304" })).toBeNull();
    expect(
      getNativeScopeView({
        ...verified,
        scope_rows_status: "verified_empty",
        scope_rows: [],
      }),
    ).toEqual({ status: "unavailable", rows: [] });
  });

  it.each([true, false])(
    "uses scoped risk and licences in comparison (available=%s)",
    async (available) => {
      vi.spyOn(kbliApi, "inspect").mockImplementation(async (code) =>
        code === "55106"
          ? available
            ? verified
            : legacy
          : { ...legacy, code, title: "Villa fixture" },
      );
      render(
        <ComparisonModal codes={codes} open={true} onOpenChange={() => {}} />,
      );
      const name = await screen.findByText("Hotel fixture");
      // The table uses one column per code; the first data cell always belongs to 55106.
      expect(name).toBeInTheDocument();
      for (const label of ["Risk Level", "Licenses"]) {
        const header = screen.getByText(label, { exact: true });
        const target = header.closest("tr")!.querySelectorAll("td")[1];
        if (available) {
          const items = within(target as HTMLElement).getAllByRole("listitem");
          expect(items).toHaveLength(6);
          for (const row of verified.scope_rows!) {
            expect(items[row.row_ordinal]).toHaveTextContent(
              row.scope_uraian + " · " + row.scale[0],
            );
            expect(items[row.row_ordinal]).toHaveTextContent(
              label === "Risk Level" ? row.risk_level : row.type,
            );
          }
        } else {
          expect(target).toHaveTextContent("Scope coverage not verified");
        }
        expect(target).not.toHaveTextContent("Legacy-only licence");
      }
    },
  );
});

describe("verified priority code scope coverage", () => {
  const codes = [
    "55201",
    "55203",
    "56101",
    "56303",
    "55209",
    "56102",
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
  const canonical = JSON.parse(
    readFileSync(
      resolve(
        __dirname,
        "../../../../../../data/source_documents/KBLI_2025_FINAL_CLEAN.json",
      ),
      "utf8",
    ),
  ).data;
  const detailFor = (code: (typeof codes)[number]): KBLIDetail => {
    const sourceRows = canonical.find(
      (record: { kode_kbli_2025: string }) => record.kode_kbli_2025 === code,
    ).per_skala;
    // Python's source pin sorts dictionary keys, including nested source values.
    const stable = (value: unknown): unknown =>
      Array.isArray(value)
        ? value.map(stable)
        : value !== null && typeof value === "object"
          ? Object.fromEntries(
              Object.entries(value)
                .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
                .map(([key, item]) => [key, stable(item)]),
            )
          : value;
    const digest = createHash("sha256")
      .update(JSON.stringify(stable(sourceRows)))
      .digest("hex");
    return {
      ...verified,
      code,
      licensing_note:
        "Historical terms require confirmation. Current terms are scope-specific.",
      scope_rows_source: {
        ...verified.scope_rows_source!,
        rows_sha256: digest,
      },
      scope_rows: sourceRows.map(
        (
          row: {
            scope_index: number;
            scope_uraian: string;
            skala_usaha: string[];
            kategori_risiko: string;
            persyaratan: string[];
            kewajiban: string[];
            kewenangan: string[];
          },
          ordinal: number,
        ) => ({
          row_ordinal: ordinal,
          scope_index: row.scope_index,
          scope_uraian: row.scope_uraian,
          scale: row.skala_usaha,
          risk_level: row.kategori_risiko,
          type: "Synthetic licence",
          requirements: row.persyaratan,
          obligations: row.kewajiban,
          authorities: row.kewenangan,
        }),
      ),
    };
  };
  it.each(codes)("keeps %s terms on their own scale", (code) => {
    const detail = detailFor(code);
    expect(getNativeScopeView(detail)?.status).toBe("available");
    render(<KBLIInspector data={detail} isLoading={false} />);
    const region = screen.getByRole("region", { name: "Licensing by scope" });
    expect(region).toHaveTextContent(detail.licensing_note!);
    expect(buildInspectorCopyLine(detail)).toContain(detail.licensing_note!);
    const rows = region.querySelectorAll("details");
    expect(rows).toHaveLength(detail.scope_rows!.length);
    detail.scope_rows!.forEach((row, ordinal) => {
      expect(rows[ordinal]).toHaveTextContent(row.scope_uraian);
      const headings = within(rows[ordinal]).getAllByRole("heading", {
        level: 4,
        hidden: true,
      });
      [row.requirements, row.obligations].forEach((terms, field) => {
        const section = headings[field].parentElement!;
        if (!terms.length)
          expect(section).toHaveTextContent("None listed for this row.");
        terms.forEach((term) => {
          const text = document.createElement("div");
          text.innerHTML = term;
          expect(section).toHaveTextContent(
            text.textContent!.replace(/\s+/g, " ").trim(),
          );
        });
      });
      expect(buildInspectorCopyLine(detail)).toContain(
        [row.scope_uraian, row.scale[0], row.risk_level].join(" · "),
      );
    });
  });
  it.each(codes)("refuses partial or misbound %s coverage", (code) => {
    const detail = detailFor(code);
    const badCases: KBLIDetail[] = [
      { ...detail, scope_rows_status: "verified_empty", scope_rows: [] },
      { ...detail, scope_rows: detail.scope_rows!.slice(0, 2) },
      { ...detail, scope_rows_source: verified.scope_rows_source },
      { ...detail, scope_rows_source: null },
      {
        ...detail,
        scope_rows: detail.scope_rows!.map((row, i) =>
          i === 2 ? { ...row, scale: ["Invalid scale"] } : row,
        ),
      },
      {
        ...detail,
        scope_rows: detail.scope_rows!.map((row, i) =>
          i === 2 ? { ...row, risk_level: "Wrong risk" } : row,
        ),
      },
    ];
    badCases.forEach((bad) =>
      expect(getNativeScopeView(bad)?.status).toBe("unavailable"),
    );
  });
});
