import { afterEach, describe, expect, it, vi } from "vitest";

// The shared test setup needs a DOM, so "no window" is simulated: the modules
// are evaluated fresh while `window` is undefined, as in server rendering.
describe("storage without a window", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("imports and works without throwing", async () => {
    vi.resetModules();
    vi.stubGlobal("window", undefined);
    expect(typeof window).toBe("undefined");

    const { safeStorage } = await import("../storage");
    await import("@/lib/auth/session-signal");

    expect(safeStorage.getItem("k")).toBeNull();
    expect(safeStorage.setItem("k", "v")).toBe(false);
    expect(() => safeStorage.removeItem("k")).not.toThrow();
    expect(() => safeStorage._onStorageEvent("k")).not.toThrow();
  });
});
