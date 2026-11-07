/**
 * Fetches that only need the client id start at MOUNT, in parallel with the
 * profile request, instead of after the profile resolves and the Overview
 * mounts (the third request wave of the page). `startEarly` begins a call and
 * remembers its promise for a few seconds; the component that owns the call
 * `takeEarly`s it once on its first load, so nothing is fetched twice. Later
 * reloads and polls find nothing and go to the network as before.
 */

const TTL_MS = 15_000;
const pending = new Map<string, { promise: Promise<unknown>; at: number }>();

export const earlyKey = (name: string, clientId: number) =>
  `${name}:${clientId}`;

export function startEarly<T>(key: string, load: () => Promise<T>): void {
  const existing = pending.get(key);
  if (existing && Date.now() - existing.at < TTL_MS) return;
  const promise = Promise.resolve(load());
  // The consumer handles rejection; this only silences a never-consumed one.
  promise.catch(() => {});
  pending.set(key, { promise, at: Date.now() });
}

/** The early promise for `key`, once; null when none is fresh. */
export function takeEarly<T>(key: string): Promise<T> | null {
  const entry = pending.get(key);
  pending.delete(key);
  if (!entry || Date.now() - entry.at > TTL_MS) return null;
  return entry.promise as Promise<T>;
}

/** Forgets every pending early call (test isolation; also a clean slate on logout). */
export function resetEarly(): void {
  pending.clear();
}
