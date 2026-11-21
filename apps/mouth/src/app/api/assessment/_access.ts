import "server-only";
import { createHmac, timingSafeEqual } from "node:crypto";
import type { NextRequest } from "next/server";
import {
  COOKIE as TAX_IV_COOKIE,
  verifyToken as verifyTaxInterviewToken,
} from "@/app/(assessment)/tax_interview/_server/access";

/**
 * Session gate for /api/assessment/submit.
 *
 * The assessment page has no PIN: GET /api/assessment/session hands any
 * same-site visitor a short-lived cookie signed with the relay secret, and the
 * page asks for it on mount. The cookie proves the request comes from a
 * browser that loaded the page on this origin — not who the visitor is. What
 * bounds abuse of the email relay is the rest of the submit contract: fixed
 * recipient, "[Assessment] " subject, size limits, same-origin Origin header
 * and the per-IP quota below.
 */

export const ASSESSMENT_COOKIE = "bz_assessment_session";
export const MAX_AGE_SECONDS = 12 * 60 * 60;

const SUBMIT_LIMIT = 10;
const SUBMIT_WINDOW_MS = 10 * 60_000;
const CANONICAL_ORIGINS = new Set([
  "https://balizero.com",
  "https://www.balizero.com",
]);

const submitHits = new Map<string, { n: number; first: number }>();

export function relaySecret(): string | null {
  const raw = process.env.INTERNAL_API_KEY || process.env.API_KEY;
  return raw && raw.trim() ? raw.trim() : null;
}

function safeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  return left.length === right.length && timingSafeEqual(left, right);
}

function sign(key: string, expiresAtMs: number): string {
  const derived = createHmac("sha256", key)
    .update("assessment-cookie-v1")
    .digest();
  return createHmac("sha256", derived)
    .update(`assessment|${expiresAtMs}`)
    .digest("base64url");
}

export function mintAssessmentToken(
  expiresAtMs = Date.now() + MAX_AGE_SECONDS * 1000,
): string | null {
  const key = relaySecret();
  return key ? `${expiresAtMs}.${sign(key, expiresAtMs)}` : null;
}

export function verifyAssessmentToken(
  token: string | undefined,
  nowMs = Date.now(),
): boolean {
  const key = relaySecret();
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

export function hasValidAssessmentSession(
  req: NextRequest,
  nowMs = Date.now(),
): boolean {
  const assessmentToken = req.cookies.get(ASSESSMENT_COOKIE)?.value;
  if (verifyAssessmentToken(assessmentToken, nowMs)) return true;

  const taxIvToken = req.cookies.get(TAX_IV_COOKIE)?.value;
  return verifyTaxInterviewToken(taxIvToken, nowMs);
}

export function sessionCookieHeader(
  req: NextRequest,
  value: string,
  maxAge: number,
): string {
  const loopback = ["localhost", "127.0.0.1", "::1"].includes(
    req.nextUrl.hostname,
  );
  const secure =
    process.env.NODE_ENV === "production" && !loopback ? "; Secure" : "";
  return `${ASSESSMENT_COOKIE}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Strict${secure}`;
}

/**
 * True when the browser-set Origin header names balizero.com, www.balizero.com
 * or the origin this request was served on (subhi.balizero.com, a preview, or
 * localhost). A missing or unparsable Origin is refused: every browser sends
 * one on a POST fetch.
 */
export function isSameOriginRequest(req: NextRequest): boolean {
  const raw = req.headers.get("origin");
  if (!raw) return false;
  let origin: string;
  try {
    origin = new URL(raw).origin;
  } catch {
    return false;
  }
  if (origin === "null") return false;
  return CANONICAL_ORIGINS.has(origin) || origin === req.nextUrl.origin;
}

export function clientIp(h: Headers): string {
  return (
    h.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    h.get("x-real-ip") ||
    "unknown"
  );
}

export function consumeSubmitQuota(ip: string, now = Date.now()): boolean {
  if (submitHits.size > 2000) submitHits.clear();
  const entry = submitHits.get(ip);
  if (!entry || now - entry.first > SUBMIT_WINDOW_MS) {
    submitHits.set(ip, { n: 1, first: now });
    return true;
  }
  if (entry.n >= SUBMIT_LIMIT) {
    return false;
  }
  entry.n += 1;
  return true;
}

export function resetAssessmentRateLimitForTests(): void {
  submitHits.clear();
}
