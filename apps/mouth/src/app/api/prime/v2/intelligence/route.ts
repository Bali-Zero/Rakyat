import { NextRequest, NextResponse } from "next/server";
import { logger } from "@/lib/logger";

const BACKEND_URL =
  process.env.NUZANTARA_API_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "https://nuzantara-rag.fly.dev";

const NO_STORE_HEADERS = { "Cache-Control": "no-store, private" };

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = request.nextUrl;
    const sw_lat = searchParams.get("sw_lat");
    const sw_lng = searchParams.get("sw_lng");
    const ne_lat = searchParams.get("ne_lat");
    const ne_lng = searchParams.get("ne_lng");

    if (!sw_lat || !sw_lng || !ne_lat || !ne_lng) {
      return NextResponse.json(
        { error: "sw_lat, sw_lng, ne_lat, ne_lng are required" },
        { status: 400, headers: NO_STORE_HEADERS },
      );
    }

    // Forward nz_access_token cookie as Bearer token
    const cookieHeader = request.headers.get("cookie") || "";
    const tokenMatch = cookieHeader.match(/nz_access_token=([^;]+)/);
    const token = tokenMatch ? tokenMatch[1] : null;

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const params = new URLSearchParams({ sw_lat, sw_lng, ne_lat, ne_lng });
    const response = await fetch(
      `${BACKEND_URL}/api/prime/v2/intelligence?${params.toString()}`,
      {
        headers,
        cache: "no-store",
      },
    );

    if (response.status === 401 || response.status === 403) {
      return NextResponse.json(
        { type: "FeatureCollection", features: [], stats: { auth_error: 1 } },
        { status: response.status, headers: NO_STORE_HEADERS },
      );
    }

    if (!response.ok) {
      logger.warn("Prime v2 intelligence failed", {
        metadata: { status: response.status, sw_lat, sw_lng, ne_lat, ne_lng },
      });
      return NextResponse.json(
        { type: "FeatureCollection", features: [], stats: {} },
        { status: response.status, headers: NO_STORE_HEADERS },
      );
    }

    const data = await response.json();
    return NextResponse.json(data, { headers: NO_STORE_HEADERS });
  } catch (error) {
    logger.error(
      "Prime v2 intelligence API error",
      {},
      error instanceof Error ? error : new Error(String(error)),
    );
    return NextResponse.json(
      { type: "FeatureCollection", features: [], stats: {} },
      { status: 502, headers: NO_STORE_HEADERS },
    );
  }
}
