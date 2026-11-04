import { NextRequest, NextResponse } from "next/server";
import { authorizeInternalBffRequest } from "@/lib/server/internal-api-bff";

export async function POST(request: NextRequest): Promise<Response> {
  const auth = await authorizeInternalBffRequest(request);
  if (auth instanceof NextResponse) {
    return auth;
  }

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json(
      { detail: "Multipart audio form data required" },
      { status: 400 },
    );
  }

  try {
    const response = await fetch(
      `${auth.backendBaseUrl}/api/audio/transcribe`,
      {
        method: "POST",
        headers: {
          "X-API-Key": auth.internalApiKey,
        },
        body: formData as unknown as BodyInit,
      },
    );

    const payload = await response
      .json()
      .catch(() => ({ detail: "Transcription failed" }));
    return NextResponse.json(payload, { status: response.status });
  } catch {
    return NextResponse.json(
      { detail: "Transcription service unavailable" },
      { status: 502 },
    );
  }
}
