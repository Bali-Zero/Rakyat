import { vi } from "vitest";

/**
 * Runs `fn` as a browser whose local timezone is `tz`.
 *
 * Setting `process.env.TZ` inside a test worker does not move the zone the
 * worker's `Date` formats in, so a formatter that forgets its own `timeZone`
 * would still pass. This replaces `Date.prototype.toLocaleDateString` with
 * one that defaults to `tz` whenever the caller names no zone, which is what
 * a real browser does, and honours a zone the caller names.
 */
export function asBrowserIn<T>(tz: string, fn: () => T): T {
  const spy = vi
    .spyOn(Date.prototype, "toLocaleDateString")
    .mockImplementation(function (
      this: Date,
      locales?: Intl.LocalesArgument,
      options?: Intl.DateTimeFormatOptions,
    ) {
      return new Intl.DateTimeFormat(locales, {
        timeZone: tz,
        ...options,
      }).format(this);
    });
  try {
    return fn();
  } finally {
    spy.mockRestore();
  }
}

/** The two zones the spec names, plus one far east. */
export const BROWSER_ZONES = [
  "America/Los_Angeles",
  "Asia/Makassar",
  "Pacific/Kiritimati",
] as const;
