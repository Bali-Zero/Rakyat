import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useDocumentUpload } from "./useDocumentUpload";

function makeFile(): File {
  return new File([new Uint8Array(1024)], "passport.jpg", {
    type: "image/jpeg",
  });
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const PROCESSING_BODY = {
  document_id: "doc-1",
  processing_state: "PROCESSING",
};

function keyOf(call: unknown[]): string {
  return ((call[1] as RequestInit).headers as Record<string, string>)[
    "Idempotency-Key"
  ];
}

describe("useDocumentUpload manual retry and reset", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    global.fetch = mockFetch;
    mockFetch.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("polls a fresh window when the customer retries after the poll budget is spent", async () => {
    vi.useFakeTimers();
    mockFetch.mockImplementation(async () =>
      jsonResponse(202, PROCESSING_BODY),
    );

    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000 * 20);
    });

    const state = result.current.state;
    expect(state.step).toBe("error");
    if (state.step === "error") {
      expect(state.message).toContain("taking longer than expected");
      expect(state.retryable).toBe(true);
    }
    const callsBeforeRetry = mockFetch.mock.calls.length;

    act(() => {
      result.current.retryUpload();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000 * 20);
    });

    expect(mockFetch.mock.calls.length - callsBeforeRetry).toBeGreaterThan(1);
    expect(keyOf(mockFetch.mock.calls[callsBeforeRetry])).toBe(
      keyOf(mockFetch.mock.calls[0]),
    );
  });

  it("leaves the idle state alone when the request it aborted rejects after a reset", async () => {
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(
              new DOMException("The operation was aborted.", "AbortError"),
            ),
          );
        }),
    );

    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    expect(result.current.state.step).toBe("uploading");

    await act(async () => {
      result.current.reset();
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(result.current.state.step).toBe("idle");
  });

  it("sends the same idempotency key when retrying after a network error", async () => {
    mockFetch
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          document_id: "doc-1",
          processing_state: "READY_FOR_REVIEW",
          review_fields: [],
        }),
      );

    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    await waitFor(() => expect(result.current.state.step).toBe("error"));

    act(() => {
      result.current.retryUpload();
    });
    await waitFor(() => expect(result.current.state.step).toBe("ready"));

    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(keyOf(mockFetch.mock.calls[1])).toBe(keyOf(mockFetch.mock.calls[0]));
  });
});
