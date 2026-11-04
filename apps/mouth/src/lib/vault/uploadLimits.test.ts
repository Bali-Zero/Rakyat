import { describe, it, expect } from "vitest";
import {
  MAX_SIZE_BYTES,
  ALLOWED_UPLOAD_MIMES,
  UPLOAD_ACCEPT,
  UPLOAD_FORMAT_LABEL,
  PLATFORM_MAX_BYTES,
  isAllowedUploadMime,
} from "./uploadLimits";

describe("uploadLimits", () => {
  it("MAX_SIZE_BYTES is a positive finite number", () => {
    expect(Number.isFinite(MAX_SIZE_BYTES)).toBe(true);
    expect(MAX_SIZE_BYTES).toBeGreaterThan(0);
  });
  it("stays under the Vercel Function body limit the upload proxy runs behind", () => {
    // Measured 19 Oct 2026: a 6 MB multipart body to /api/portal/documents/upload
    // gets 413 FUNCTION_PAYLOAD_TOO_LARGE from the platform; 1 MB reaches the backend.
    expect(MAX_SIZE_BYTES).toBeLessThanOrEqual(4 * 1024 * 1024);
    expect(MAX_SIZE_BYTES).toBe(PLATFORM_MAX_BYTES);
  });
  it("contains only formats parsed by the backend", () => {
    expect(ALLOWED_UPLOAD_MIMES).toEqual([
      "application/pdf",
      "image/jpeg",
      "image/png",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]);
    expect(UPLOAD_ACCEPT).toBe(".pdf,.jpg,.jpeg,.png,.docx");
    expect(UPLOAD_FORMAT_LABEL).toBe("PDF, JPG, PNG, or DOCX up to 4 MB");
  });
  it("isAllowedUploadMime accepts known types", () => {
    expect(isAllowedUploadMime("application/pdf")).toBe(true);
    expect(isAllowedUploadMime("image/png")).toBe(true);
  });
  it("isAllowedUploadMime rejects unknown types", () => {
    expect(isAllowedUploadMime("application/x-msdownload")).toBe(false);
    expect(isAllowedUploadMime("text/html")).toBe(false);
    expect(isAllowedUploadMime("image/webp")).toBe(false);
    expect(isAllowedUploadMime("application/msword")).toBe(false);
    expect(
      isAllowedUploadMime(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      ),
    ).toBe(false);
    expect(isAllowedUploadMime("")).toBe(false);
  });
});
