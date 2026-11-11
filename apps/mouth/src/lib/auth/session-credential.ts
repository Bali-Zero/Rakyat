import { safeStorage } from "@/lib/utils/storage";
import { getTokenSubject, isTokenExpired } from "@/lib/utils/token";
import {
  AUTH_TOKEN_STORAGE_KEY,
  USER_PROFILE_STORAGE_KEY,
} from "./session-signal";

/**
 * FRESH synchronous reads of the stored credential. Spec: ./SESSION.md S3/S4.
 *
 * These read storage directly on every call. They deliberately do not use the
 * epoch or any event: another tab's `storage` events are queued and their
 * order relative to a fetch continuation is implementation-defined, so only
 * what storage holds at this instant is evidence. Pure: nothing is removed and
 * nothing is dispatched. Tokens and subs are never logged.
 */

export const TOKEN_CLOCK_SKEW_SECONDS = 30;

/** The raw stored token, valid or not, or null. */
export function readStoredToken(): string | null {
  try {
    return safeStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** The stored token if it is present and not expired, else null. */
export function readStoredValidToken(): string | null {
  const token = readStoredToken();
  if (!token || isTokenExpired(token, TOKEN_CLOCK_SKEW_SECONDS)) return null;
  return token;
}

/** `sub` of the currently valid stored token, or null. */
export function readStoredValidSub(): string | null {
  return getTokenSubject(readStoredValidToken());
}

/** The stored profile, only if its `id` is `sub` (never someone else's). */
export function readStoredProfileFor<P extends { id?: unknown }>(
  sub: string,
): P | null {
  try {
    const raw = safeStorage.getItem(USER_PROFILE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as P | null;
    return parsed && String(parsed.id) === sub ? parsed : null;
  } catch {
    return null;
  }
}
