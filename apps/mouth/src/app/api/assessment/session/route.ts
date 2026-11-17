import { NextRequest, NextResponse } from "next/server";
import {
  MAX_AGE_SECONDS,
  mintAssessmentToken,
  sessionCookieHeader,
} from "../_access";

/**
 * GET /api/assessment/session
 *
 * Sets the signed, HttpOnly, SameSite=Strict cookie that
 * /api/assessment/submit requires. The assessment page calls it on mount.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function jsonResponse(body: unknown, status: number): NextResponse {
  const res = NextResponse.json(body, { status });
  res.headers.set("Cache-Control", "no-store, private");
  return res;
}

export async function GET(req: NextRequest): Promise<NextResponse> {
  const token = mintAssessmentToken(Date.now() + MAX_AGE_SECONDS * 1000);
  if (!token) {
    return jsonResponse({ ok: false, error: "NOT_CONFIGURED" }, 503);
  }

  const res = jsonResponse({ ok: true }, 200);
  res.headers.set(
    "Set-Cookie",
    sessionCookieHeader(req, token, MAX_AGE_SECONDS),
  );
  return res;
}
