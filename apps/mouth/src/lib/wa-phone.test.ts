import { describe, expect, it } from "vitest";
import { normalizeWaPhone } from "./wa-phone";

describe("normalizeWaPhone", () => {
  it("strips a leading + and the separators clients type", () => {
    expect(normalizeWaPhone("+62 890-0000-0000")).toBe("6289000000000");
    expect(normalizeWaPhone("+62 (890) 0000.0000")).toBe("6289000000000");
  });

  it("turns a national leading 0 into 62", () => {
    expect(normalizeWaPhone("0890 0000 0000")).toBe("6289000000000");
    expect(normalizeWaPhone("089000000000")).toBe("6289000000000");
  });

  it("keeps an already-stored number unchanged", () => {
    expect(normalizeWaPhone("6289000000000")).toBe("6289000000000");
  });

  it("returns null for numbers the backend would still refuse", () => {
    expect(normalizeWaPhone("62notaphone")).toBeNull();
    expect(normalizeWaPhone("abc")).toBeNull();
    expect(normalizeWaPhone("++6289000000000")).toBeNull();
    expect(normalizeWaPhone("006289000000000")).toBeNull();
    expect(normalizeWaPhone("+62 89")).toBeNull();
    expect(normalizeWaPhone("+62 890 0000 0000 0000")).toBeNull();
    expect(normalizeWaPhone("")).toBeNull();
    expect(normalizeWaPhone(null)).toBeNull();
    expect(normalizeWaPhone(undefined)).toBeNull();
  });
});
