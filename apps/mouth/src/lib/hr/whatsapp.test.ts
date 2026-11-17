import { describe, expect, it } from "vitest";
import { WHATSAPP_MESSAGE, checkWhatsapp, maskWhatsapp } from "./whatsapp";

// Synthetic numbers only.
const MASKED = "62•••••1234";

describe("maskWhatsapp", () => {
  it("keeps the 62 prefix and the last four digits", () => {
    expect(maskWhatsapp("6289000001234")).toBe(MASKED);
    expect(maskWhatsapp("+62 890-0000-1234")).toBe(MASKED);
  });
  it("is idempotent on what the backend sends", () => {
    expect(maskWhatsapp(MASKED)).toBe(MASKED);
    expect(maskWhatsapp(maskWhatsapp("6289000001234"))).toBe(MASKED);
  });
  it("never shows more than six digits", () => {
    const out = maskWhatsapp("628900000000000") ?? "";
    expect(out.replace(/[^0-9]/g, "")).toHaveLength(6);
  });
  it("shows nothing for an empty, missing or too-short value", () => {
    expect(maskWhatsapp(null)).toBeNull();
    expect(maskWhatsapp(undefined)).toBeNull();
    expect(maskWhatsapp("")).toBeNull();
    expect(maskWhatsapp("1234567")).toBeNull();
  });
});

describe("checkWhatsapp", () => {
  it("normalises separators and a leading +", () => {
    expect(checkWhatsapp("+62 (890) 0000.0001")).toEqual({
      ok: true,
      value: "6289000000001",
    });
  });
  it("treats empty as unchanged", () => {
    expect(checkWhatsapp("")).toEqual({ ok: true, value: null });
    expect(checkWhatsapp("   ")).toEqual({ ok: true, value: null });
  });
  it("accepts 10 to 15 digits starting with 62", () => {
    expect(checkWhatsapp("6289000000")).toEqual({
      ok: true,
      value: "6289000000",
    });
    expect(checkWhatsapp("628900000000000")).toEqual({
      ok: true,
      value: "628900000000000",
    });
  });
  it.each([
    "0890000000001",
    "628900000",
    "6289000000000000",
    "62-890-abc-0001",
    "62٠890000001",
  ])("refuses %s without echoing it", (bad) => {
    const res = checkWhatsapp(bad);
    expect(res).toEqual({ ok: false, message: WHATSAPP_MESSAGE });
    if (!res.ok) expect(res.message).not.toContain(bad);
  });
});
