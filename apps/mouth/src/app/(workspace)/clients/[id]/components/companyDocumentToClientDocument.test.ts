import { describe, expect, it } from "vitest";
import type { CompanyDocument } from "@/lib/api/crm/crm.types";
import { companyDocumentToClientDocument } from "./companyDocumentToClientDocument";
import { sptFilings } from "./tax-deadlines";

const cd = (over: Partial<CompanyDocument>): CompanyDocument => ({
  id: 5,
  uuid: "u-5",
  company_id: 1,
  document_type: "spt_tahunan_badan",
  status: "active",
  is_verified: true,
  created_at: "2026-03-01T00:00:00Z",
  file_name: "return.pdf",
  google_drive_file_url: "https://drive.google.com/file/d/f1/view",
  ...over,
});

describe("companyDocumentToClientDocument", () => {
  it("GUILT: fiscal_year survives the adapter, so return.pdf counts for 2025", () => {
    const mapped = companyDocumentToClientDocument(
      cd({ fiscal_year: 2025 }),
      7,
    );
    expect(mapped.fiscal_year).toBe(2025);
    expect(
      sptFilings([mapped], "spt_company").map((f) => f.fiscalYear),
    ).toEqual([2025]);
  });

  it("INNOCENCE: without fiscal_year and without a year in the name, no filing", () => {
    const mapped = companyDocumentToClientDocument(cd({}), 7);
    expect(sptFilings([mapped], "spt_company")).toEqual([]);
  });
});
