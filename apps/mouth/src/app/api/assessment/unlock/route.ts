import { NextRequest, NextResponse } from "next/server";
import {
  ASSESSMENT_COOKIE,
  GUESS_DELAY_MS,
  MAX_AGE_SECONDS,
  clearUnlockFailures,
  clientIp,
  isUnlockLocked,
  mintAssessmentToken,
  pinMatches,
  recordUnlockFailure,
  relaySecret,
  sleep,
} from "../_access";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function cookieHeader(
  req: NextRequest,
  value: string,
  maxAge: number,
): string {
  const loopback = ["localhost", "127.0.0.1", "::1"].includes(
    req.nextUrl.hostname,
  );
  const secure =
    process.env.NODE_ENV === "production" && !loopback ? "; Secure" : "";
  return `${ASSESSMENT_COOKIE}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${secure}`;
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

export async function POST(req: NextRequest): Promise<NextResponse> {
  if (!relaySecret()) {
    return jsonResponse({ ok: false, error: "NOT_CONFIGURED" }, 503);
  }

  const ip = clientIp(req.headers);
  if (isUnlockLocked(ip)) {
    return jsonResponse({ ok: false, error: "TOO_MANY_ATTEMPTS" }, 429, {
      "Retry-After": "300",
    });
  }

  let pin = "";
  try {
    const body: unknown = await req.json();
    if (
      body &&
      typeof body === "object" &&
      typeof (body as { pin?: unknown }).pin === "string"
    ) {
      pin = (body as { pin: string }).pin.trim();
    }
  } catch {
    // Malformed body counts as an invalid attempt.
  }

  await sleep(GUESS_DELAY_MS);
  if (!pin || !pinMatches(pin)) {
    recordUnlockFailure(ip);
    return jsonResponse({ ok: false, error: "INVALID_PIN" }, 401);
  }

  clearUnlockFailures(ip);
  const token = mintAssessmentToken(Date.now() + MAX_AGE_SECONDS * 1000);
  if (!token) {
    return jsonResponse({ ok: false, error: "NOT_CONFIGURED" }, 503);
  }

  const res = jsonResponse({ ok: true }, 200);
  res.headers.set("Set-Cookie", cookieHeader(req, token, MAX_AGE_SECONDS));
  return res;
}

export async function DELETE(req: NextRequest): Promise<NextResponse> {
  const res = jsonResponse({ ok: true }, 200);
  res.headers.set("Set-Cookie", cookieHeader(req, "", 0));
  return res;
}
