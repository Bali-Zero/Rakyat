import { NextRequest, NextResponse } from "next/server";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";

// Same backend resolution order as the other mouth BFF handlers
// (NUZANTARA_API_URL is the deployed variable; ZANTARA_API_URL is legacy).
// Ignore relative "/api" values so server-side Node fetch always receives an origin.
function resolveBackendBaseUrl(): string {
  for (const raw of [
    process.env.NUZANTARA_API_URL,
    process.env.NEXT_PUBLIC_API_URL,
    process.env.ZANTARA_API_URL,
  ]) {
    const trimmed = raw?.trim();
    if (trimmed && /^https?:\/\//i.test(trimmed)) {
      return trimmed.replace(/\/api\/?$/, "").replace(/\/+$/, "");
    }
  }
  return "https://nuzantara-rag.fly.dev";
}

/**
 * GET /api/blog/newsletter/confirm
 * Confirm newsletter subscription via email link
 */
export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const token = searchParams.get("token");

    if (!token) {
      return NextResponse.redirect(
        new URL(
          "/insights?newsletter=error&message=missing-token",
          request.url,
        ),
      );
    }

    const response = await fetch(
      `${resolveBackendBaseUrl()}/api/blog/newsletter/confirm?token=${encodeURIComponent(token)}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
      },
    );

    if (!response.ok) {
      const errorCode =
        response.status === 404 ? "invalid-token" : "confirmation-failed";
      return NextResponse.redirect(
        new URL(`/insights?newsletter=error&message=${errorCode}`, request.url),
      );
    }

    // Redirect to success page
    return NextResponse.redirect(
      new URL("/insights?newsletter=confirmed", request.url),
    );
  } catch (error) {
    logger.error(
      "Newsletter confirmation error",
      { component: "Newsletter", action: "confirm" },
      toError(error),
    );
    return NextResponse.redirect(
      new URL("/insights?newsletter=error&message=server-error", request.url),
    );
  }
}

/**
 * POST /api/blog/newsletter/confirm
 * JSON confirmation `{ subscriberId, token }` (NewsletterService.confirmSubscription).
 * A file-system route that exports only GET shadows the `/api/[...path]`
 * catch-all and answered POST with 405; forward it to the backend instead.
 */
export async function POST(request: NextRequest) {
  try {
    const payload = await request.json().catch(() => null);
    const subscriberId = payload?.subscriberId;
    const token = payload?.token;

    if (typeof subscriberId !== "string" || typeof token !== "string") {
      return NextResponse.json(
        { detail: "subscriberId and token are required" },
        { status: 422 },
      );
    }

    const response = await fetch(`${resolveBackendBaseUrl()}/api/blog/newsletter/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscriberId, token }),
    });

    const text = await response.text();
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = { detail: text || "Unexpected upstream response" };
    }

    return NextResponse.json(body ?? {}, { status: response.status });
  } catch (error) {
    logger.error(
      "Newsletter confirmation error",
      { component: "Newsletter", action: "confirm_post" },
      toError(error),
    );
    return NextResponse.json(
      { detail: "Newsletter confirmation failed" },
      { status: 502 },
    );
  }
}
