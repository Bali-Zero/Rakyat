import {
  AUTH_TOKEN_STORAGE_KEY,
  notifySessionChanged,
} from "@/lib/auth/session-signal";

/**
 * JWT Token Utilities
 *
 * Provides token expiry validation without a full JWT library.
 * Uses base64 decoding of the JWT payload to check the `exp` claim.
 */

/**
 * Decode a JWT payload without verification.
 * This is safe for expiry checking -- signature verification
 * happens on the backend.
 *
 * @returns Parsed payload or null if decoding fails
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;

    // Base64url decode the payload (second segment)
    const payload = parts[1]
      .replace(/-/g, "+")
      .replace(/_/g, "/");

    const decoded = atob(payload);
    return JSON.parse(decoded);
  } catch {
    return null;
  }
}

/**
 * The one clock-skew grace, in seconds, used wherever the client decides
 * whether a stored token is still valid (isTokenExpired's default, the session
 * identity, getProfile's freshness check). Do not introduce a second number.
 */
export const TOKEN_CLOCK_SKEW_SECONDS = 30;

/**
 * The bearer token in a FINAL headers object, exactly as it will be passed to
 * fetch. Header names are case-insensitive, so every key spelling counts; more
 * than one Authorization entry (fetch would join them), a non-Bearer scheme or
 * a malformed value gives null. Never throws, never logs.
 */
export function bearerTokenFromHeaders(
  headers: Record<string, string> | undefined,
): string | null {
  if (!headers) return null;
  const values = Object.keys(headers)
    .filter((k) => k.toLowerCase() === "authorization")
    .map((k) => headers[k]);
  if (values.length !== 1 || typeof values[0] !== "string") return null;
  const match = /^Bearer ([^\s,]+)$/i.exec(values[0].trim());
  return match ? match[1] : null;
}

/**
 * The `sub` claim of a JWT as a non-empty string, or null (undecodable, no
 * `sub`, non-string `sub`). No signature check; never throws.
 */
export function getTokenSubject(token: string | null | undefined): string | null {
  if (!token) return null;
  const sub = decodeJwtPayload(token)?.sub;
  return typeof sub === "string" && sub.length > 0 ? sub : null;
}

/**
 * Check if a JWT token is expired.
 *
 * @param token - JWT token string
 * @param clockSkewSeconds - Grace period in seconds (default 30s)
 * @returns true if the token has a valid `exp` claim that is in the past,
 *          false if the token is still valid, not a JWT, or has no exp claim.
 *          Non-JWT tokens are treated as valid (backend will reject if invalid).
 */
export function isTokenExpired(
  token: string,
  clockSkewSeconds: number = TOKEN_CLOCK_SKEW_SECONDS,
): boolean {
  if (!token) return true;

  const payload = decodeJwtPayload(token);
  if (!payload) {
    // Not a decodable JWT -- let backend validate; do not reject client-side
    return false;
  }

  const exp = payload.exp;
  if (typeof exp !== "number") {
    // No `exp` claim -- treat as non-expiring (some internal tokens)
    return false;
  }

  const nowSeconds = Math.floor(Date.now() / 1000);
  return nowSeconds >= exp - clockSkewSeconds;
}

/**
 * Get a token from storage, validating its expiry.
 * If the token is expired, it is removed from storage and null is returned.
 *
 * @param key - Storage key (e.g. "auth_token")
 * @param storage - Storage interface (localStorage or safeStorage)
 * @returns The token if valid, or null if expired/missing
 */
export function getValidToken(
  key: string,
  storage: {
    getItem: (k: string) => string | null;
    removeItem: (k: string) => void;
  },
): string | null {
  const token = storage.getItem(key);
  if (!token) return null;

  if (isTokenExpired(token)) {
    storage.removeItem(key);
    // The bearer token left this tab without the profile and without any
    // other signal: tell the pages bound to it (SESSION.md S2).
    if (key === AUTH_TOKEN_STORAGE_KEY) notifySessionChanged();
    return null;
  }

  return token;
}
