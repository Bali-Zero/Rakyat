import { NextRequest, NextResponse } from "next/server";
import {
  COOKIE,
  GUESS_DELAY_MS,
  MAX_AGE_SECONDS,
  clearFailures,
  clientIp,
  isLocked,
  mintToken,
  pinMatches,
  recordFailure,
  secret,
  sleep,
} from "@/app/(assessment)/tax_interview/_server/access";

/**
 * POST { pin } -> sets the signed HttpOnly `bz_tax_iv` cookie (401 wrong PIN,
 * 429 throttled, 503 when the server secret is missing: fail closed).
 * DELETE -> clears it. Deliberately outside `/api/assessment/*` and never
 * proxied to the backend: the PIN is compared here and is never logged.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const cookie = (req: NextRequest, value: string, maxAge: number): string => {
  const loopback = ["localhost", "127.0.0.1", "::1"].includes(
    req.nextUrl.hostname,
  );
  const secure =
    process.env.NODE_ENV === "production" && !loopback ? "; Secure" : "";
  // Host-only (no Domain), as the Visa Oracle unlock does.
  return `${COOKIE}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${secure}`;
};

const json = (body: unknown, status: number): NextResponse => {
  const res = NextResponse.json(body, { status });
  res.headers.set("Cache-Control", "no-store, private");
  return res;
};

export async function POST(req: NextRequest): Promise<NextResponse> {
  if (!secret()) return json({ ok: false, error: "NOT_CONFIGURED" }, 503);

  const ip = clientIp(req.headers);
  if (isLocked(ip)) return json({ ok: false, error: "TOO_MANY_ATTEMPTS" }, 429);

  let pin = "";
  try {
    const body: unknown = await req.json();
    if (
      body &&
      typeof body === "object" &&
      typeof (body as { pin?: unknown }).pin === "string"
    )
      pin = (body as { pin: string }).pin.trim();
  } catch {
    // A malformed body is just a failed attempt.
  }

  await sleep(GUESS_DELAY_MS);
  if (!pin || !pinMatches(pin)) {
    recordFailure(ip);
    return json({ ok: false, error: "INVALID_PIN" }, 401);
  }

  clearFailures(ip);
  const token = mintToken(Date.now() + MAX_AGE_SECONDS * 1000);
  if (!token) return json({ ok: false, error: "NOT_CONFIGURED" }, 503);
  const res = json({ ok: true }, 200);
  res.headers.set("Set-Cookie", cookie(req, token, MAX_AGE_SECONDS));
  return res;
}

export async function DELETE(req: NextRequest): Promise<NextResponse> {
  const res = json({ ok: true }, 200);
  res.headers.set("Set-Cookie", cookie(req, "", 0));
  return res;
}
