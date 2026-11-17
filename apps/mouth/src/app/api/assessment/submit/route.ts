import { NextRequest, NextResponse } from "next/server";
import { logger } from "@/lib/logger";

/**
 * POST /api/assessment/submit
 *
 * Receives block answers from the assessment page and forwards them
 * as an email to zero@balizero.com via the backend notification service.
 *
 * This route exists so the browser never needs the backend API key.
 * Temporary — will be removed after the assessment.
 */

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

interface SubmitBody {
  to: string;
  subject: string;
  body: string;
}

export async function POST(request: NextRequest) {
  try {
    const payload: SubmitBody = await request.json();

    // Only allow sending to zero@balizero.com
    if (payload.to !== "zero@balizero.com") {
      return NextResponse.json({ error: "Invalid recipient" }, { status: 400 });
    }

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
      return NextResponse.json(
        {
          error: "Email relay is not configured",
          detail:
            "INTERNAL_API_KEY is missing from this deployment's environment.",
        },
        { status: 503 },
      );
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
      return NextResponse.json(
        { error: "Failed to send email", detail: text },
        { status: res.status },
      );
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
      return NextResponse.json(
        { success: false, error: reason },
        { status: 502 },
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    logger.error("[assessment/submit] Error:", {
      component: "assessment",
      action: "submit",
      metadata: { error: String(error) },
    });
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 },
    );
  }
}
