import { NextRequest, NextResponse } from "next/server";
import {
  authorizeInternalBffRequest,
  EDITORIAL_APPROVER_ROLES,
} from "@/lib/server/internal-api-bff";

const ALLOWED_STAGING_TYPES = new Set(["visa", "news"]);
const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ type: string; id: string }> },
): Promise<Response> {
  const { type, id } = await params;

  if (!ALLOWED_STAGING_TYPES.has(type)) {
    return NextResponse.json(
      { detail: "Invalid staging type" },
      { status: 400 },
    );
  }

  if (!id || !SAFE_ID_PATTERN.test(id) || id.includes("..")) {
    return NextResponse.json(
      { detail: "Invalid staging item id" },
      { status: 400 },
    );
  }

  const auth = await authorizeInternalBffRequest(request, {
    allowedRoles: EDITORIAL_APPROVER_ROLES,
  });
  if (auth instanceof NextResponse) {
    return auth;
  }

  const rawBody = await request.text().catch(() => "");
  const headers: Record<string, string> = {
    "X-API-Key": auth.internalApiKey,
  };
  if (rawBody) {
    headers["Content-Type"] =
      request.headers.get("content-type") || "application/json";
  }

  try {
    const response = await fetch(
      `${auth.backendBaseUrl}/api/intel/staging/approve/${encodeURIComponent(type)}/${encodeURIComponent(id)}`,
      {
        method: "POST",
        headers,
        body: rawBody || undefined,
      },
    );

    const payload = await response
      .json()
      .catch(() => ({ detail: "Upstream response error" }));
    return NextResponse.json(payload, { status: response.status });
  } catch {
    return NextResponse.json(
      { detail: "Failed to reach intelligence approval service" },
      { status: 502 },
    );
  }
}
