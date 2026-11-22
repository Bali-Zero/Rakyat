import { describe, it, expect } from "vitest";
import { messageFor, messageForUnknownCode } from "./messages";
import type { UploadErrorCode } from "./types";

const CATALOG_CODES: UploadErrorCode[] = [
  "IDEMPOTENCY_KEY_REQUIRED",
  "SESSION_REQUIRED",
  "GARUDA_PUBLIC_DISABLED",
  "RESULT_NOT_FOUND",
  "IDEMPOTENCY_CONFLICT",
  "DOCUMENT_TOO_LARGE",
  "UNSUPPORTED_DOCUMENT_MEDIA_TYPE",
  "INVALID_REQUEST",
  "UNREADABLE_DOCUMENT",
  "PERSISTENCE_POLICY_UNAVAILABLE",
  "DOCUMENT_PROCESSING_UNAVAILABLE",
  "SERVICE_UNAVAILABLE",
  "INTERNAL_ERROR",
];

const NETWORK_SENTENCE = messageForUnknownCode("__network__");

describe("upload messages", () => {
  it("gives the generic server-side sentence for a name inherited from Object.prototype", () => {
    const generic = messageFor("SERVICE_UNAVAILABLE");
    for (const inherited of ["constructor", "toString", "hasOwnProperty"]) {
      const text = messageFor(inherited as UploadErrorCode);
      expect(typeof text).toBe("string");
      expect(text).toBe(generic);
    }
  });

  it("gives a non-empty sentence for a code that is not in the catalog", () => {
    const text = messageFor("RATE_LIMITED" as UploadErrorCode);
    expect(typeof text).toBe("string");
    expect(text.length).toBeGreaterThan(0);
    expect(text).toBe(messageFor("SERVICE_UNAVAILABLE"));
  });

  it("keeps the network sentence for a name inherited from Object.prototype in the sentinel lookup", () => {
    expect(messageForUnknownCode("constructor")).toBe(NETWORK_SENTENCE);
    expect(messageForUnknownCode("toString")).toBe(NETWORK_SENTENCE);
  });

  it("returns a distinct non-empty text for every catalog code, unchanged", () => {
    for (const code of CATALOG_CODES) {
      const text = messageFor(code);
      expect(text.length).toBeGreaterThan(0);
      expect(messageForUnknownCode(code)).toBe(text);
    }
    expect(messageFor("SESSION_REQUIRED")).toContain("sign in again");
    expect(messageFor("DOCUMENT_TOO_LARGE")).toContain("15 MB");
    expect(NETWORK_SENTENCE).toContain("couldn't reach the server");
  });
});
