/** Test helpers for session tests. Never used by production code. */

export function makeJwt(payload: Record<string, unknown>): string {
  const body = btoa(JSON.stringify(payload))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
  return `header.${body}.signature`;
}

/** A JWT for `sub` that expires `ttlSeconds` from now (default 1 h). */
export function tokenFor(sub: string, ttlSeconds = 3600): string {
  return makeJwt({ sub, exp: Math.floor(Date.now() / 1000) + ttlSeconds });
}
