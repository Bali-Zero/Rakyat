import { NextRequest, NextResponse } from "next/server";
import { authorizeInternalBffRequest } from "@/lib/server/internal-api-bff";

export async function POST(request: NextRequest): Promise<Response> {
  const auth = await authorizeInternalBffRequest(request);
  if (auth instanceof NextResponse) {
    return auth;
  }

  let bodyText: string;
  try {
    bodyText = await request.text();
  } catch {
    return NextResponse.json(
      { detail: "Invalid speech synthesis payload" },
      { status: 400 },
    );
  }

  try {
    const response = await fetch(`${auth.backendBaseUrl}/api/audio/speech`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": auth.internalApiKey,
      },
      body: bodyText,
    });

    if (!response.ok) {
      const errPayload = await response
        .json()
        .catch(() => ({ detail: "Speech generation failed" }));
      return NextResponse.json(errPayload, { status: response.status });
    }

    const audioBuffer = await response.arrayBuffer();
    return new Response(audioBuffer, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") || "audio/mpeg",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "Speech synthesis service unavailable" },
      { status: 502 },
    );
  }
}
