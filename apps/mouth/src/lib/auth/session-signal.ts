/**
 * Session signal + epoch. Spec: ./SESSION.md (S2).
 *
 * Every write or removal of the BEARER TOKEN (`auth_token`) calls
 * `notifySessionChanged()` from the single place that does it. That bumps an
 * in-memory per-tab epoch and dispatches the same-tab window event
 * `bz:session-changed`. Writes to `user_profile` do NOT signal: the identity
 * here is the token, and a profile write must not reload a bound page.
 * Another tab's token change reaches this tab as a `storage` event on the same
 * key, which bumps the epoch here too.
 *
 * Nothing in this module reads or logs a token value.
 */

export const AUTH_TOKEN_STORAGE_KEY = "auth_token";
export const SESSION_CHANGED_EVENT = "bz:session-changed";

let epoch = 0;

/** Current session epoch of this tab. Changes on every bearer token write or removal. */
export function getSessionEpoch(): number {
  return epoch;
}

/** Bump the epoch and tell this tab. Call right AFTER the storage mutation. */
export function notifySessionChanged(): void {
  epoch += 1;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(SESSION_CHANGED_EVENT));
  }
}

/** True when a `storage` event concerns the bearer token (key === null = storage.clear()). */
export function isSessionStorageEvent(event: StorageEvent): boolean {
  return event.key === null || event.key === AUTH_TOKEN_STORAGE_KEY;
}

if (typeof window !== "undefined") {
  // Installed once per module instance: another tab changed the bearer token.
  window.addEventListener("storage", (event) => {
    if (isSessionStorageEvent(event)) epoch += 1;
  });
}
