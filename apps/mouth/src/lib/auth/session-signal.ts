/**
 * Session signal + epoch. Spec: ./SESSION.md (S2, S3).
 *
 * Every write or removal of the bearer token or of `user_profile` calls
 * `notifySessionChanged()` from the single place that does it. That bumps an
 * in-memory per-tab epoch and dispatches the same-tab window event
 * `bz:session-changed`. Another tab's change reaches this tab as a `storage`
 * event on the same keys, which bumps the epoch here too.
 *
 * Nothing in this module reads or logs a token value.
 */

// Side-effect import, deliberate: it makes storage.ts evaluate (and register
// its `storage` listener) before this module registers its own, so a fallback
// copy is settled before any session listener reads. See storage.ts.
import "@/lib/utils/storage";

export const AUTH_TOKEN_STORAGE_KEY = "auth_token";
export const USER_PROFILE_STORAGE_KEY = "user_profile";
export const SESSION_CHANGED_EVENT = "bz:session-changed";

let epoch = 0;

/** Current session epoch of this tab. Changes on every token/profile write or removal. */
export function getSessionEpoch(): number {
  return epoch;
}

/**
 * What changed: the bearer token (default), a `user_profile` write, or a
 * `user_profile` removal by the invariant enforcer ("enforced", so in-memory
 * copies of the profile can be dropped with it).
 */
export type SessionChangeKind = "token" | "profile" | "enforced";

/**
 * Bump the epoch and tell this tab. Call right AFTER the storage mutation.
 * `kind` rides in the event detail: the profile invariant enforcer does not
 * treat the profile write that completes a login as a violation.
 */
export function notifySessionChanged(kind: SessionChangeKind = "token"): void {
  epoch += 1;
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent(SESSION_CHANGED_EVENT, { detail: { kind } }),
    );
  }
}

/** True when a `storage` event concerns a session key (key === null = storage.clear()). */
export function isSessionStorageEvent(event: StorageEvent): boolean {
  return (
    event.key === null ||
    event.key === AUTH_TOKEN_STORAGE_KEY ||
    event.key === USER_PROFILE_STORAGE_KEY
  );
}

if (typeof window !== "undefined") {
  // Installed once per module instance: another tab changed the session.
  window.addEventListener("storage", (event) => {
    if (isSessionStorageEvent(event)) epoch += 1;
  });
}
