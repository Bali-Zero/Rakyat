/**
 * IndexNow API Route - Push-based indexing for search engines
 * Notifies Bing, Yandex, and other IndexNow-compatible engines of content updates
 * Reduces AI citation delay from 14-28 days to <7 days
 *
 * Usage: POST /api/indexnow with { urls: ["/business/my-article"] }
 * Auth: Requires INDEXNOW_SECRET env var matching Authorization header
 */

import { timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

const INDEXNOW_KEY = "2633309a0003ec408c59ec48c952604f";
const ALLOWED_HOSTS = new Set(["balizero.com", "www.balizero.com"]);
const DEFAULT_BASE_URL = "https://balizero.com";
const MAX_INDEXNOW_URLS = 10000;

function safeTokenEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  return left.length === right.length && timingSafeEqual(left, right);
}

function canonicalBaseUrl(): string {
  const raw = (process.env.NEXT_PUBLIC_PUBLIC_URL || DEFAULT_BASE_URL)
    .trim()
    .replace(/\/+$/, "");
  try {
    const parsed = new URL(raw);
    if (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.port &&
      ALLOWED_HOSTS.has(parsed.hostname.toLowerCase())
    ) {
      return parsed.origin;
    }
  } catch {
    // Fall back to default canonical host.
  }
  return DEFAULT_BASE_URL;
}

function normalizeAllowedUrl(rawUrl: unknown, canonicalBase: string): string | null {
  if (typeof rawUrl !== "string") return null;
  const trimmed = rawUrl.trim();
  if (
    !trimmed ||
    trimmed.startsWith("//") ||
    trimmed.includes("\\") ||
    /[\r\n\x00-\x1f\x7f]/.test(trimmed)
  ) {
    return null;
  }

  let parsed: URL;
  try {
    if (/^https?:\/\//i.test(trimmed)) {
      parsed = new URL(trimmed);
    } else if (trimmed.startsWith("/")) {
      parsed = new URL(trimmed, canonicalBase);
    } else {
      return null;
    }
  } catch {
    return null;
  }

  if (
    parsed.protocol !== "https:" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.port !== "" ||
    !ALLOWED_HOSTS.has(parsed.hostname.toLowerCase())
  ) {
    return null;
  }

  return parsed.toString();
}

export async function POST(request: NextRequest) {
  // Fail-closed: refuse unless INDEXNOW_SECRET is configured and Bearer matches.
  const secret = process.env.INDEXNOW_SECRET?.trim();
  if (!secret) {
    return NextResponse.json(
      { error: "IndexNow is not configured" },
      { status: 503 },
    );
  }

  const auth = request.headers.get("authorization") || "";
  const bearerPrefix = "Bearer ";
  const presented = auth.startsWith(bearerPrefix)
    ? auth.slice(bearerPrefix.length)
    : "";
  if (!presented || !safeTokenEquals(presented, secret)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const body = (await request.json()) as { urls?: unknown };
    const urls = body?.urls;

    if (
      !Array.isArray(urls) ||
      urls.length === 0 ||
      urls.length > MAX_INDEXNOW_URLS
    ) {
      return NextResponse.json(
        { error: "urls array required (1-10000 items)" },
        { status: 400 },
      );
    }

    const base = canonicalBaseUrl();
    const absoluteUrls: string[] = [];
    for (const rawUrl of urls) {
      const normalized = normalizeAllowedUrl(rawUrl, base);
      if (!normalized) {
        return NextResponse.json(
          {
            error:
              "Invalid URL: only https://balizero.com and https://www.balizero.com are allowed",
          },
          { status: 400 },
        );
      }
      absoluteUrls.push(normalized);
    }

    // Submit to IndexNow (Bing endpoint - shared with Yandex, Seznam, etc.)
    const response = await fetch("https://api.indexnow.org/indexnow", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: new URL(base).hostname,
        key: INDEXNOW_KEY,
        keyLocation: `${base}/${INDEXNOW_KEY}.txt`,
        urlList: absoluteUrls,
      }),
    });

    return NextResponse.json({
      submitted: absoluteUrls.length,
      status: response.status,
      ok: response.ok,
    });
  } catch {
    return NextResponse.json(
      { error: "Failed to submit to IndexNow" },
      { status: 500 },
    );
  }
}
