import { NextResponse } from "next/server";

const AUTH_COOKIE_NAME = "nz_access_token";
const AUTH_PROFILE_TIMEOUT_MS = 5_000;
const EXTERNAL_ROLES = new Set(["client", "partner"]);
const INTERNAL_EMAIL_DOMAINS = [
  "@balizero.com",
  "@zantara.io",
  "@nuzantara.com",
];
// Synchronized with `TEAM_ROLES` in `apps/backend-rag/backend/app/utils/service_accounts.py`
const INTERNAL_ROLES = new Set([
  "accounting",
  "admin",
  "board",
  "board member",
  "ceo",
  "consultant",
  "executive consultant",
  "finance",
  "founder",
  "junior consultant",
  "marketing",
  "marketing & accounting",
  "marketing advisory",
  "member",
  "operations",
  "ops",
  "owner",
  "reception",
  "sales",
  "specialist advisor",
  "supervisor",
  "tax",
  "tax care",
  "tax lead",
  "tax manager",
  "team",
  "team leader",
  "zero",
]);

interface UserProfileLike {
  email?: unknown;
  role?: unknown;
  is_team_member?: unknown;
}

export interface AuthorizedInternalBffContext {
  backendBaseUrl: string;
  internalApiKey: string;
  sessionToken: string;
}

export function getInternalBffBackendBaseUrl(): string {
  const raw =
    process.env.NUZANTARA_API_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "https://nuzantara-rag.fly.dev";
  return raw
    .trim()
    .replace(/\/+$/, "")
    .replace(/\/api$/, "");
}

export function getInternalBffApiKey(): string | undefined {
  const explicit =
    process.env.NUZANTARA_INTERNAL_API_KEY?.trim() ||
    process.env.NUZANTARA_API_KEY?.trim() ||
    process.env.VOICE_CONCIERGE_BACKEND_API_KEY?.trim();
  if (explicit) return explicit;

  const firstFromList = process.env.API_KEYS?.split(",")[0]?.trim();
  return firstFromList || undefined;
}

export function extractSessionToken(request: Request): string | undefined {
  const authorization = request.headers.get("authorization");
  const bearerToken = authorization?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim();
  if (bearerToken) return bearerToken;

  const cookieHeader = request.headers.get("cookie");
  if (!cookieHeader) return undefined;

  for (const part of cookieHeader.split(";")) {
    const [rawName, ...rawValue] = part.trim().split("=");
    if (rawName !== AUTH_COOKIE_NAME) continue;
    const value = rawValue.join("=").trim();
    if (value) return value;
  }

  return undefined;
}

function isInternalStaffProfile(profile: UserProfileLike | undefined): boolean {
  if (!profile || typeof profile !== "object") return false;

  const role =
    typeof profile.role === "string" ? profile.role.trim().toLowerCase() : "";
  const email =
    typeof profile.email === "string" ? profile.email.trim().toLowerCase() : "";

  if (role && EXTERNAL_ROLES.has(role)) return false;
  if (profile.is_team_member === true) return true;
  if (role && INTERNAL_ROLES.has(role)) return true;

  if (email && INTERNAL_EMAIL_DOMAINS.some((d) => email.endsWith(d))) {
    return true;
  }

  return false;
}

function extractProfile(payload: unknown): UserProfileLike | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const record = payload as Record<string, unknown>;
  if (record.data && typeof record.data === "object") {
    return record.data as UserProfileLike;
  }
  return record as UserProfileLike;
}

export const EDITORIAL_APPROVER_ROLES: ReadonlySet<string> = new Set([
  "admin",
  "board",
  "board member",
  "ceo",
  "consultant",
  "executive consultant",
  "founder",
  "junior consultant",
  "marketing",
  "marketing & accounting",
  "marketing advisory",
  "owner",
  "specialist advisor",
  "supervisor",
  "tax lead",
  "tax manager",
  "team",
  "team leader",
  "zero",
]);

export interface AuthorizeInternalBffOptions {
  allowedRoles?: ReadonlySet<string>;
}

/**
 * Verify that the incoming browser request belongs to an authenticated internal
 * staff session before attaching the server-side X-API-Key.
 */
export async function authorizeInternalBffRequest(
  request: Request,
  options?: AuthorizeInternalBffOptions,
): Promise<AuthorizedInternalBffContext | NextResponse> {
  const sessionToken = extractSessionToken(request);
  if (!sessionToken) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const backendBaseUrl = getInternalBffBackendBaseUrl();
  try {
    const profileRes = await fetch(`${backendBaseUrl}/api/auth/profile`, {
      method: "GET",
      headers: { Authorization: `Bearer ${sessionToken}` },
      redirect: "error",
      signal: AbortSignal.timeout(AUTH_PROFILE_TIMEOUT_MS),
    });

    if (profileRes.status === 401) {
      return NextResponse.json(
        { detail: "Invalid or expired session" },
        { status: 401 },
      );
    }

    if (!profileRes.ok) {
      return NextResponse.json(
        { detail: "Staff authorization verification failed" },
        { status: 403 },
      );
    }

    const payload = (await profileRes.json().catch(() => null)) as unknown;
    const profile = extractProfile(payload);
    if (!isInternalStaffProfile(profile)) {
      return NextResponse.json(
        { detail: "Team member access required" },
        { status: 403 },
      );
    }
    if (options?.allowedRoles) {
      const normalizedRole =
        typeof profile?.role === "string"
          ? profile.role.trim().toLowerCase()
          : "";
      if (!normalizedRole || !options.allowedRoles.has(normalizedRole)) {
        return NextResponse.json(
          { detail: "Editorial or admin role required" },
          { status: 403 },
        );
      }
    }
  } catch {
    return NextResponse.json(
      { detail: "Authentication service unavailable" },
      { status: 502 },
    );
  }

  const internalApiKey = getInternalBffApiKey();
  if (!internalApiKey) {
    return NextResponse.json(
      { detail: "Internal API key not configured" },
      { status: 503 },
    );
  }

  return {
    backendBaseUrl,
    internalApiKey,
    sessionToken,
  };
}
