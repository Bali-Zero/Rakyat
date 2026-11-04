import { describe, expect, it } from "vitest";
import {
  BASE64_UPLOAD_MAX_BYTES,
  BASE64_UPLOAD_MAX_LABEL,
  PROXY_BODY_LIMIT_BYTES,
  base64Size,
} from "./base64UploadLimit";

describe("base64 upload cap", () => {
  it("a file at the cap, base64-encoded, still fits the proxy body limit with room for JSON", () => {
    expect(base64Size(BASE64_UPLOAD_MAX_BYTES) + 64 * 1024).toBeLessThan(
      PROXY_BODY_LIMIT_BYTES,
    );
  });

  it("GUILT: the old 10 MB promise could not fit (13.3 MB of base64)", () => {
    expect(base64Size(10 * 1024 * 1024)).toBeGreaterThan(
      PROXY_BODY_LIMIT_BYTES,
    );
  });

  it("the label matches the number", () => {
    expect(BASE64_UPLOAD_MAX_LABEL).toBe(
      `${BASE64_UPLOAD_MAX_BYTES / 1024 / 1024} MB`,
    );
  });
});
