import { safeStorage } from "@/lib/utils/storage";
import {
  TOKEN_CLOCK_SKEW_SECONDS,
  getTokenSubject,
  isTokenExpired,
} from "@/lib/utils/token";
import {
  AUTH_TOKEN_STORAGE_KEY,
  SESSION_CHANGED_EVENT,
  USER_PROFILE_STORAGE_KEY,
  isSessionStorageEvent,
  notifySessionChanged,
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

/**
 * Invariant (SESSION.md S6): `user_profile` belongs to the sub of the
 * currently valid stored token, or is absent. Removes a profile that breaks
 * it, through a path that signals, and says whether it did. A valid token whose
 * sub cannot be decoded (not a JWT) cannot be judged and keeps the profile.
 * Idempotent: with nothing to remove it does nothing and signals nothing, so
 * the signal its own removal fires cannot loop.
 *
 * `allowProfileWithoutToken` is for the signal fired by a profile write itself:
 * a profile with NO valid token is tolerated there (a login that wrote the
 * profile first, and the client's own unit of work), while a profile that
 * contradicts a valid token is removed on every trigger.
 */
export function enforceProfileInvariant(
  options: { allowProfileWithoutToken?: boolean } = {},
): boolean {
  let raw: string | null;
  try {
    raw = safeStorage.getItem(USER_PROFILE_STORAGE_KEY);
  } catch {
    return false;
  }
  if (!raw) return false;
  const token = readStoredValidToken();
  let breaks = false;
  if (!token) {
    breaks = !options.allowProfileWithoutToken;
  } else {
    const sub = getTokenSubject(token);
    if (sub) {
      try {
        breaks = String((JSON.parse(raw) as { id?: unknown })?.id) !== sub;
      } catch {
        breaks = true;
      }
    }
  }
  if (!breaks) return false;
  safeStorage.removeItem(USER_PROFILE_STORAGE_KEY);
  notifySessionChanged("enforced");
  return true;
}

let enforcerInstalled = false;

/**
 * Run enforceProfileInvariant on every session signal, session-key `storage`
 * event, focus and visibility->visible. Installed once per page load from
 * every module that writes session state (the ApiClient base and the public
 * auth client, so also the portal login) and from the identity helper.
 */
export function installProfileInvariantEnforcer(): void {
  if (enforcerInstalled || typeof window === "undefined") return;
  enforcerInstalled = true;
  const run = () => {
    enforceProfileInvariant();
  };
  window.addEventListener(SESSION_CHANGED_EVENT, (event) => {
    const kind = (event as CustomEvent<{ kind?: string }>).detail?.kind;
    enforceProfileInvariant({
      allowProfileWithoutToken: kind === "profile" || kind === "enforced",
    });
  });
  window.addEventListener("storage", (event) => {
    if (isSessionStorageEvent(event)) run();
  });
  window.addEventListener("focus", run);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") run();
  });
}
