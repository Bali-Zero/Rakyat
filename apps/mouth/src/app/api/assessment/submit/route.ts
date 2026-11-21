import { NextRequest, NextResponse } from "next/server";
import { logger } from "@/lib/logger";
import {
  clientIp,
  consumeSubmitQuota,
  hasValidAssessmentSession,
  isSameOriginRequest,
} from "../_access";

/**
 * POST /api/assessment/submit
 *
 * Receives block answers from the assessment page and forwards them
 * as an email to zero@balizero.com via the backend notification service.
 * Requires a same-origin Origin header and the session cookie that
 * GET /api/assessment/session sets when the page mounts.
 *
 * This route exists so the browser never needs the backend API key.
 * Temporary — will be removed after the assessment.
 */

const SUBJECT_PREFIX = "[Assessment] ";
const MAX_SUBJECT_CHARS = 200;
const MAX_BODY_CHARS = 65_536;
// Outer bound on the raw JSON before parsing: a body at MAX_BODY_CHARS can
// grow several-fold once UTF-8 encoded and JSON-escaped.
const MAX_REQUEST_BYTES = 512 * 1024;

// Read at request time, never at module scope. A module-scope read is
// evaluated once per lambda build/boot, so a corrected secret does not reach a
// running deployment until something forces a fresh build — which is invisible,
// because the symptom is a 401 from the backend that looks like a backend
// problem. Measured 2026-09-01: the key on Vercel was the one revoked on
// 2026-07-12, and two redeploys did not pick up the replacement.
function backendUrl(): string {
  return (
    process.env.NUZANTARA_API_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "https://nuzantara-rag.fly.dev"
  );
}

function apiKey(): string {
  return process.env.INTERNAL_API_KEY || process.env.API_KEY || "";
}

function jsonResponse(
  body: unknown,
  status: number,
  extraHeaders?: Record<string, string>,
): NextResponse {
  const res = NextResponse.json(body, { status, headers: extraHeaders });
  res.headers.set("Cache-Control", "no-store, private");
  return res;
}

interface SubmitBody {
  to: string;
  subject: string;
  body: string;
}

export async function POST(request: NextRequest) {
  try {
    // Say plainly that the deployment has no credential, instead of forwarding
    // an empty header and letting the backend answer 401. That 401 reads as
    // "the backend rejected us" and sends the reader hunting in the wrong
    // service; this names the actual cause, in the only place that can see it.
    const key = apiKey();
    if (!key) {
      logger.error(
        "[assessment/submit] INTERNAL_API_KEY is not set on this deployment — refusing to send",
        { component: "assessment", action: "submit" },
      );
      return jsonResponse(
        {
          error: "Email relay is not configured",
          detail:
            "INTERNAL_API_KEY is missing from this deployment's environment.",
        },
        503,
      );
    }

    if (!isSameOriginRequest(request)) {
      return jsonResponse({ error: "Forbidden: cross-origin request" }, 403);
    }

    if (!hasValidAssessmentSession(request)) {
      return jsonResponse(
        { error: "Unauthorized: valid assessment session required" },
        401,
      );
    }

    const ip = clientIp(request.headers);
    if (!consumeSubmitQuota(ip)) {
      return jsonResponse({ error: "Too many requests" }, 429, {
        "Retry-After": "60",
      });
    }

    const declaredLength = request.headers.get("content-length");
    if (declaredLength !== null) {
      const parsedLength = Number(declaredLength);
      if (Number.isFinite(parsedLength) && parsedLength > MAX_REQUEST_BYTES) {
        return jsonResponse({ error: "Payload too large" }, 413);
      }
    }

    const rawBody = await request.text();
    if (Buffer.byteLength(rawBody, "utf8") > MAX_REQUEST_BYTES) {
      return jsonResponse({ error: "Payload too large" }, 413);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(rawBody);
    } catch {
      return jsonResponse({ error: "Invalid JSON payload" }, 400);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return jsonResponse({ error: "Invalid request body" }, 400);
    }

    const payload = parsed as Partial<SubmitBody>;

    // Only allow sending to zero@balizero.com
    if (payload.to !== "zero@balizero.com") {
      return jsonResponse({ error: "Invalid recipient" }, 400);
    }

    if (
      typeof payload.subject !== "string" ||
      !payload.subject.startsWith(SUBJECT_PREFIX) ||
      /[\r\n]/.test(payload.subject)
    ) {
      return jsonResponse({ error: "Invalid subject" }, 400);
    }
    if (payload.subject.length > MAX_SUBJECT_CHARS) {
      return jsonResponse({ error: "Subject too large" }, 413);
    }

    if (typeof payload.body !== "string" || !payload.body.trim()) {
      return jsonResponse({ error: "Invalid body" }, 400);
    }
    if (payload.body.length > MAX_BODY_CHARS) {
      return jsonResponse({ error: "Body too large" }, 413);
    }

    // Forward to backend
    const base = backendUrl()
      .replace(/\/+$/, "")
      .replace(/\/api$/, "");
    const res = await fetch(`${base}/api/notifications/send-email`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": key,
      },
      body: JSON.stringify({
        to: payload.to,
        subject: payload.subject,
        body: payload.body,
      }),
    });

    if (!res.ok) {
      const text = await res.text();
      logger.error(`[assessment/submit] Backend error ${res.status}: ${text}`, {
        component: "assessment",
        action: "submit",
      });
      return jsonResponse({ error: "Failed to send email", detail: text }, res.status);
    }

    const data = await res.json();

    // The backend answers HTTP 200 with `{ success: false, message }` when no
    // mail provider is configured or every provider failed (see
    // notifications/router.py send_email). Relaying that as a 200 told the
    // browser the panel received an email nobody got, and the candidate's
    // exercise was retired as "submitted". A dropped email is a failed
    // delivery: surface it as 502 so the client's `res.ok` check holds.
    if (data && typeof data === "object" && data.success === false) {
      const reason =
        typeof data.message === "string" && data.message
          ? data.message
          : "Email delivery failed";
      logger.error(`[assessment/submit] Backend reported delivery failure`, {
        component: "assessment",
        action: "submit",
        metadata: { reason },
      });
      return jsonResponse({ success: false, error: reason }, 502);
    }

    return jsonResponse(data, 200);
  } catch (error) {
    logger.error("[assessment/submit] Error:", {
      component: "assessment",
      action: "submit",
      metadata: { error: String(error) },
    });
    return jsonResponse({ error: "Internal server error" }, 500);
  }
}
