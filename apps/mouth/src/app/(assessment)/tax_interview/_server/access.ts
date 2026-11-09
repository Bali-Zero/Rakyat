import "server-only";
import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * PIN gate for /tax_interview.
 *
 * HONEST LIMIT: three digits are 1000 guesses. This is a soft gate that keeps a
 * casual visitor and a search crawler out of a candidate-facing page; it is NOT
 * authentication and must never guard anything whose disclosure would harm. The
 * throttle below (fixed delay + a per-IP counter held in lambda memory, so it is
 * not shared between instances) raises the cost of a script, it does not stop a
 * determined one. The test content is fictional and carries no client data.
 *
 * The PIN is a constant by the owner's choice: no env var to be missing on
 * interview day. The cookie is signed with a key derived from the SERVER secret
 * the email relay already uses, never from the PIN (a key derived from a 3-digit
 * PIN would make forging a cookie as cheap as guessing). Rotating that secret or
 * the PIN invalidates every outstanding cookie. Without the secret the gate
 * fails closed: no unlock, no verify.
 */
const PIN = "666";

export const COOKIE = "bz_tax_iv";
export const MAX_AGE_SECONDS = 12 * 60 * 60;
export const GUESS_DELAY_MS = 400;
const FAIL_LIMIT = 10;
const FAIL_WINDOW_MS = 5 * 60_000;

const tries = new Map<string, { n: number; first: number }>();

export const sleep = (ms: number) =>
  new Promise<void>((r) => setTimeout(r, ms));

export function secret(): string | null {
  const raw = process.env.INTERNAL_API_KEY || process.env.API_KEY;
  return raw && raw.trim() ? raw.trim() : null;
}

function safeEquals(a: string, b: string): boolean {
  const l = Buffer.from(a, "utf8");
  const r = Buffer.from(b, "utf8");
  return l.length === r.length && timingSafeEqual(l, r);
}

export const pinMatches = (presented: string): boolean =>
  safeEquals(presented, PIN);

function sign(key: string, expiresAtMs: number): string {
  const k = createHmac("sha256", key)
    .update("tax-interview-cookie-v1")
    .digest();
  return createHmac("sha256", k)
    .update(`tax-interview|${expiresAtMs}|${PIN}`)
    .digest("base64url");
}

export function mintToken(expiresAtMs: number): string | null {
  const key = secret();
  return key ? `${expiresAtMs}.${sign(key, expiresAtMs)}` : null;
}

export function verifyToken(
  token: string | undefined,
  nowMs = Date.now(),
): boolean {
  const key = secret();
  if (!key || !token) return false;
  const dot = token.indexOf(".");
  if (dot <= 0) return false;
  const exp = token.slice(0, dot);
  const presented = token.slice(dot + 1);
  if (!/^\d{1,15}$/.test(exp) || !presented) return false;
  const expiresAtMs = Number(exp);
  if (!Number.isSafeInteger(expiresAtMs) || expiresAtMs <= nowMs) return false;
  return safeEquals(presented, sign(key, expiresAtMs));
}

export function clientIp(h: Headers): string {
  return (
    h.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    h.get("x-real-ip") ||
    "unknown"
  );
}

export function isLocked(ip: string, now = Date.now()): boolean {
  const t = tries.get(ip);
  if (!t) return false;
  if (now - t.first > FAIL_WINDOW_MS) {
    tries.delete(ip);
    return false;
  }
  return t.n >= FAIL_LIMIT;
}

export function recordFailure(ip: string, now = Date.now()): void {
  if (tries.size > 2000) tries.clear();
  const t = tries.get(ip);
  if (!t || now - t.first > FAIL_WINDOW_MS) tries.set(ip, { n: 1, first: now });
  else t.n += 1;
}

export const clearFailures = (ip: string): void => void tries.delete(ip);

export const resetThrottleForTests = (): void => tries.clear();
