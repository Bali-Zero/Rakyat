import "server-only";
import { createHmac, timingSafeEqual } from "node:crypto";
import type { NextRequest } from "next/server";
import {
  COOKIE as TAX_IV_COOKIE,
  verifyToken as verifyTaxInterviewToken,
} from "@/app/(assessment)/tax_interview/_server/access";

export const ASSESSMENT_COOKIE = "bz_assessment_session";
export const MAX_AGE_SECONDS = 12 * 60 * 60;
export const GUESS_DELAY_MS = 200;

const FAIL_LIMIT = 10;
const FAIL_WINDOW_MS = 5 * 60_000;
const SUBMIT_LIMIT = 10;
const SUBMIT_WINDOW_MS = 10 * 60_000;

const unlockTries = new Map<string, { n: number; first: number }>();
const submitHits = new Map<string, { n: number; first: number }>();

export const sleep = (ms: number): Promise<void> =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

export function relaySecret(): string | null {
  const raw = process.env.INTERNAL_API_KEY || process.env.API_KEY;
  return raw && raw.trim() ? raw.trim() : null;
}

function safeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  return left.length === right.length && timingSafeEqual(left, right);
}

export function pinMatches(presented: string): boolean {
  const candidatePin =
    process.env.ASSESSMENT_PIN?.trim() || "SUNSET-2026";
  const internalPin =
    process.env.ASSESSMENT_INTERNAL_PIN?.trim() || "BZ-2026";
  const matchCandidate = safeEquals(presented, candidatePin);
  const matchInternal = safeEquals(presented, internalPin);
  return matchCandidate || matchInternal;
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

export function clientIp(h: Headers): string {
  return (
    h.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    h.get("x-real-ip") ||
    "unknown"
  );
}

export function isUnlockLocked(ip: string, now = Date.now()): boolean {
  const entry = unlockTries.get(ip);
  if (!entry) return false;
  if (now - entry.first > FAIL_WINDOW_MS) {
    unlockTries.delete(ip);
    return false;
  }
  return entry.n >= FAIL_LIMIT;
}

export function recordUnlockFailure(ip: string, now = Date.now()): void {
  if (unlockTries.size > 2000) unlockTries.clear();
  const entry = unlockTries.get(ip);
  if (!entry || now - entry.first > FAIL_WINDOW_MS) {
    unlockTries.set(ip, { n: 1, first: now });
  } else {
    entry.n += 1;
  }
}

export function clearUnlockFailures(ip: string): void {
  unlockTries.delete(ip);
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
  unlockTries.clear();
  submitHits.clear();
}
