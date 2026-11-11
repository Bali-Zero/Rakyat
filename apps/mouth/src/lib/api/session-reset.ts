/**
 * Session boundaries, shared by every client that can change who is signed in
 * (the shared api client and the public login client), so whoever caches
 * per-account data in the browser drops it at login, logout, token expiry or a
 * change of the identity the portal reports.
 */
const listeners = new Set<() => void>();
let lastIdentity: string | null = null;

function runListeners(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // one failing listener must not keep the others from clearing
    }
  }
}

/** Subscribe to session boundaries; returns the unsubscribe function. */
export function onSessionReset(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** A credential was set or cleared: everything cached belongs to someone else. */
export function notifySessionReset(): void {
  lastIdentity = null;
  runListeners();
}

/**
 * The portal reports who is signed in (covers cookie-only sessions, which set
 * no token). The first identity seen is recorded; a different one resets.
 */
export function noteSessionIdentity(identity: string): void {
  const changed = lastIdentity !== null && lastIdentity !== identity;
  lastIdentity = identity;
  if (changed) runListeners();
}
