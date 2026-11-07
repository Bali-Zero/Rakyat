import { describe, expect, it } from "vitest";
import { isImeComposing } from "./ime-composing";

describe("isImeComposing", () => {
  it("is false for a plain Enter", () => {
    expect(isImeComposing({ keyCode: 13, isComposing: false })).toBe(false);
    expect(
      isImeComposing({ keyCode: 13, nativeEvent: { isComposing: false } }),
    ).toBe(false);
  });

  it("reads a React synthetic event through nativeEvent", () => {
    expect(
      isImeComposing({ keyCode: 13, nativeEvent: { isComposing: true } }),
    ).toBe(true);
  });

  it("reads a native KeyboardEvent", () => {
    expect(isImeComposing({ keyCode: 13, isComposing: true })).toBe(true);
  });

  it("treats Safari's keyCode 229 as composing", () => {
    expect(isImeComposing({ keyCode: 229, isComposing: false })).toBe(true);
    expect(
      isImeComposing({ keyCode: 229, nativeEvent: { isComposing: false } }),
    ).toBe(true);
  });
});
