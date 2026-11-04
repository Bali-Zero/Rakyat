/**
 * Guard — kita client-page uploads are sent as base64 inside JSON to
 * `/api/crm/clients/{id}/documents/upload` through the Next proxy, which
 * refuses bodies above ~4.5 MB. None of them may promise or allow 10 MB;
 * each must use the shared cap from `lib/upload/base64UploadLimit`.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const FILES = [
  "FamilyTab.tsx",
  "PassportCard.tsx",
  "VisaCard.tsx",
  "company/CompanyDocUpload.tsx",
  "modals/AddDocumentModal.tsx",
];

describe.each(FILES)("%s", (file) => {
  const src = readFileSync(join(__dirname, file), "utf8");

  it("GUILT: no 10 MB size check or label", () => {
    expect(src).not.toMatch(/10 \* 1024 \* 1024/);
    expect(src).not.toMatch(/10 ?MB/);
  });

  it("uses the shared base64 cap", () => {
    expect(src).toContain("BASE64_UPLOAD_MAX_BYTES");
  });
});
