"use client";

import { useEffect, useMemo, useRef } from "react";
import { parseCalendarDate } from "./documentValidity";
import { witaDateKey } from "./overview-brief";

/**
 * The card's clock. The day of every document derivation is the `as_of` of
 * the profile payload on screen; this module is, with `overview-brief.ts`, the
 * only reader of the browser clock on the validity path. The browser clock
 * never sets the day: it only notices that the payload is stale and asks for
 * a fresh one.
 */

export const CLOCK_TICK_MS = 60_000;

/**
 * The card's day. The page passes the payload's day down as a `day` prop; a
 * component rendered alone (no prop) reads the WITA day of the browser, the
 * same fallback the page itself uses when the payload carries no `as_of`.
 */
export function resolveCardDay(day?: string | null): string {
  return day ?? witaDateKey(Date.now());
}

/**
 * The day of the profile payload being shown: its `as_of`, else (an older
 * backend) the WITA day at the moment the payload was received. One value for
 * the whole card.
 */
export function useProfileDay(
  profile: { as_of?: string | null } | null | undefined,
): string | null {
  return useMemo(() => {
    if (!profile) return null;
    return parseCalendarDate(profile.as_of) ?? witaDateKey(Date.now());
  }, [profile]);
}

/** The instant a card day starts: 00:00 in WITA (UTC+8, no DST). */
export function dayToInstant(day: string): number {
  return Date.parse(`${day}T00:00:00+08:00`);
}

/** Milliseconds until the next 00:00 WITA after `nowMs`. */
export function msUntilNextWitaMidnight(nowMs: number): number {
  const startOfToday = dayToInstant(witaDateKey(nowMs));
  return startOfToday + 86_400_000 - nowMs;
}

/**
 * Asks for fresh data when the browser's WITA day has moved past the card's
 * day: every `CLOCK_TICK_MS`, on window focus, and once at the next 00:00
 * WITA. Until the new payload lands the card keeps its old `as_of` everywhere.
 */
export function useWitaClock(day: string | null, onStale: () => void): void {
  const callback = useRef(onStale);
  callback.current = onStale;
  useEffect(() => {
    if (!day) return;
    const check = () => {
      if (witaDateKey(Date.now()) > day) callback.current();
    };
    const tick = setInterval(check, CLOCK_TICK_MS);
    const midnight = setTimeout(
      check,
      msUntilNextWitaMidnight(Date.now()) + 1_000,
    );
    window.addEventListener("focus", check);
    return () => {
      clearInterval(tick);
      clearTimeout(midnight);
      window.removeEventListener("focus", check);
    };
  }, [day]);
}
