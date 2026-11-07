import { vi } from "vitest";

/**
 * 12:00 WITA on 2026-06-15 (04:00Z). At this instant the UTC date and the
 * WITA date are the same day, so a fixture built as "N days from now" with
 * `toISOString().slice(0, 10)` counts the same N days as the components,
 * which count against the WITA calendar day. Under the real clock the two
 * dates differ between 16:00Z and 24:00Z.
 */
export const FIXED_NOW = new Date("2026-06-15T04:00:00.000Z");

/** Fakes `Date` only (timers stay real) at `FIXED_NOW`. Pair with `vi.useRealTimers()`. */
export function useFixedClock(): void {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXED_NOW);
}
