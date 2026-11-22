import { describe, expect, it, vi } from "vitest";
import { evaluateVisaOracle, VisaOracleClientError } from "./evaluation-client";
import { makeVisaOracleResponse } from "./visa-oracle-test-fixture";
import type { VisaOracleEvaluateRequest } from "./visa-oracle-contract";

const IDEMPOTENCY_KEY = "11111111-1111-4111-8111-111111111111";

const REQUEST = {
  schema_version: "1.0.0",
  assessment_id: "22222222-2222-4222-8222-222222222222",
  collected_at: "2026-08-03T04:00:00.000Z",
  facts: {},
  disclosed_review_flags: [],
} as unknown as VisaOracleEvaluateRequest;

const JSON_HEADERS = { "content-type": "application/json" };

function droppedBodyResponse(): Response {
  const body = new ReadableStream<Uint8Array>({
    pull() {
      throw new Error("connection reset");
    },
  });
  return new Response(body, { status: 200, headers: JSON_HEADERS });
}

function textResponse(text: string): Response {
  return new Response(text, { status: 200, headers: JSON_HEADERS });
}

async function captureError(
  fetchImpl: typeof fetch,
  maxRetries: number,
): Promise<unknown> {
  try {
    await evaluateVisaOracle({
      request: REQUEST,
      idempotencyKey: IDEMPOTENCY_KEY,
      fetchImpl,
      maxRetries,
      waitBeforeRetry: async () => undefined,
    });
  } catch (error) {
    return error;
  }
  return undefined;
}

describe("Visa Oracle evaluation client body read failures", () => {
  it("retries a response whose body read fails and surfaces a network failure", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => droppedBodyResponse());
    const error = await captureError(fetchImpl, 1);

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(error).toBeInstanceOf(VisaOracleClientError);
    expect((error as VisaOracleClientError).code).toBe("NETWORK_FAILURE");
  });

  it("recovers when the retry after a failed body read succeeds", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(droppedBodyResponse())
      .mockResolvedValueOnce(
        textResponse(JSON.stringify(makeVisaOracleResponse())),
      );
    const result = await evaluateVisaOracle({
      request: REQUEST,
      idempotencyKey: IDEMPOTENCY_KEY,
      fetchImpl,
      maxRetries: 1,
      waitBeforeRetry: async () => undefined,
    });

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(result.mode).toBe("ENGINE");
  });

  it("keeps a completely read body that is not valid JSON as malformed and does not retry", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      textResponse("{not json"),
    );
    const error = await captureError(fetchImpl, 1);

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect((error as VisaOracleClientError).code).toBe("MALFORMED_RESPONSE");
  });

  it("keeps a completely read body with invalid UTF-8 as malformed and does not retry", async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(new Uint8Array([0x7b, 0xff, 0xfe, 0x7d]), {
          status: 200,
          headers: JSON_HEADERS,
        }),
    );
    const error = await captureError(fetchImpl, 1);

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect((error as VisaOracleClientError).code).toBe("MALFORMED_RESPONSE");
  });
});
