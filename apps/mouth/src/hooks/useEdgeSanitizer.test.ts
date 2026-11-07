import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useEdgeSanitizer } from "./useEdgeSanitizer";

vi.mock("@/lib/logger", () => ({
  logger: {
    debug: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

describe("useEdgeSanitizer", () => {
  it("reports isReady as true and status as ready", () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    expect(result.current.isReady).toBe(true);
    expect(result.current.status).toBe("ready");
  });

  it("returns empty string for empty input", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const sanitized = await result.current.sanitize("");
    expect(sanitized).toBe("");
  });

  it("redacts email addresses", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const sanitized = await result.current.sanitize(
      "Contact us at info@balizero.com for details",
    );
    expect(sanitized).toBe("Contact us at [EMAIL] for details");
    expect(sanitized).not.toContain("info@balizero.com");
  });

  it("redacts Indonesian phone numbers (+62)", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const sanitized = await result.current.sanitize(
      "Call +6284000220000 for info",
    );
    expect(sanitized).toContain("[PHONE]");
    expect(sanitized).not.toContain("+6284000220000");
  });

  it("redacts Indonesian phone numbers (08 prefix)", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const sanitized = await result.current.sanitize("WhatsApp 084000220000");
    expect(sanitized).toContain("[PHONE]");
    expect(sanitized).not.toContain("084000220000");
  });

  it("leaves non-PII text unchanged", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const text = "We offer visa services in Bali for expats.";
    const sanitized = await result.current.sanitize(text);
    expect(sanitized).toBe(text);
  });

  it("redacts multiple PII types in one string", async () => {
    const { result } = renderHook(() => useEdgeSanitizer());
    const sanitized = await result.current.sanitize(
      "Email: john@example.com, Phone: +6284000250000",
    );
    expect(sanitized).toContain("[EMAIL]");
    expect(sanitized).toContain("[PHONE]");
    expect(sanitized).not.toContain("john@example.com");
    expect(sanitized).not.toContain("+6284000250000");
  });
});
