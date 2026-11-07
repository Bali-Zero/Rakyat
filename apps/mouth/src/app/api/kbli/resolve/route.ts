import { NextResponse } from "next/server";
import { resolveKbliCodes } from "@/lib/kbli-resolve";

const MAX_CODES = 60;

export async function GET(request: Request) {
  const raw = new URL(request.url).searchParams.get("codes") ?? "";
  const codes = Array.from(
    new Set(
      raw
        .split(",")
        .map((c) => c.trim())
        .filter((c) => /^\d{5}$/.test(c)),
    ),
  ).slice(0, MAX_CODES);

  return NextResponse.json(
    { resolutions: resolveKbliCodes(codes) },
    { headers: { "Cache-Control": "no-store" } },
  );
}
