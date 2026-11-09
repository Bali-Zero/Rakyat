import { NextRequest, NextResponse } from "next/server";
import { logger } from "@/lib/logger";
import {
  COOKIE,
  secret,
  verifyToken,
} from "@/app/(assessment)/tax_interview/_server/access";
import {
  buildReport,
  cleanTelemetry,
} from "@/app/(assessment)/tax_interview/_server/report";
import {
  normalizeAnswers,
  scoreAll,
} from "@/app/(assessment)/tax_interview/_server/score";

/**
 * POST /api/tax-interview/submit
 *
 * Scores on the server (the browser never holds a key) and sends ONE email to
 * the panel through the backend relay. Nothing is stored: no table, no file.
 * The candidate gets `{ ok: true }` and never a score. The attempt id doubles as
 * the relay's idempotency key, so a retry after a lost response cannot send twice.
 * Never log the name, the answers or the PIN.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const TO = "zero@balizero.com";
const CC = "ari.firda@balizero.com,angel.tax@balizero.com";
const MAX_BODY_CHARS = 120_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Read at request time, never at module scope (see api/assessment/submit).
const backendUrl = (): string =>
  (
    process.env.NUZANTARA_API_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "https://nuzantara-rag.fly.dev"
  )
    .replace(/\/+$/, "")
    .replace(/\/api$/, "");

const json = (body: unknown, status: number): NextResponse => {
  const res = NextResponse.json(body, { status });
  res.headers.set("Cache-Control", "no-store, private");
  return res;
};

const ts = (v: unknown): number => {
  const n = typeof v === "number" && Number.isFinite(v) ? v : Date.now();
  return Math.min(Math.max(n, 0), Date.now() + 86_400_000);
};

export async function POST(req: NextRequest): Promise<NextResponse> {
  if (!verifyToken(req.cookies.get(COOKIE)?.value))
    return json({ ok: false, error: "UNAUTHORIZED" }, 401);
  const key = secret();
  if (!key) return json({ ok: false, error: "NOT_CONFIGURED" }, 503);

  let body: Record<string, unknown>;
  try {
    const text = await req.text();
    if (text.length > MAX_BODY_CHARS)
      return json({ ok: false, error: "TOO_LARGE" }, 413);
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== "object") throw new Error("shape");
    body = parsed as Record<string, unknown>;
  } catch {
    return json({ ok: false, error: "BAD_REQUEST" }, 400);
  }

  const attemptId = typeof body.attemptId === "string" ? body.attemptId : "";
  const name =
    typeof body.name === "string" ? body.name.trim().slice(0, 80) : "";
  if (!UUID.test(attemptId) || !name)
    return json({ ok: false, error: "BAD_REQUEST" }, 400);

  const answers = normalizeAnswers(body.answers);
  const telemetry = cleanTelemetry(body.telemetry);
  const { subject, html } = buildReport({
    name,
    attemptId,
    startedAt: ts(body.startedAt),
    endedAt: ts(body.endedAt),
    answers,
    telemetry,
    score: scoreAll(answers),
  });

  try {
    const res = await fetch(`${backendUrl()}/api/notifications/send-email`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": key },
      body: JSON.stringify({
        to: TO,
        cc: CC,
        subject,
        body: html,
        idempotency_key: attemptId,
      }),
    });
    if (!res.ok) {
      logger.error(`[tax-interview/submit] relay answered ${res.status}`, {
        component: "tax-interview",
        action: "submit",
      });
      return json({ ok: false, error: "SEND_FAILED" }, 502);
    }
    const ack: unknown = await res.json().catch(() => null);
    if (
      ack &&
      typeof ack === "object" &&
      (ack as { success?: unknown }).success === false
    )
      return json({ ok: false, error: "SEND_FAILED" }, 502);
    return json({ ok: true }, 200);
  } catch (error) {
    logger.error("[tax-interview/submit] relay unreachable", {
      component: "tax-interview",
      action: "submit",
      metadata: { error: error instanceof Error ? error.name : "unknown" },
    });
    return json({ ok: false, error: "SEND_FAILED" }, 502);
  }
}
