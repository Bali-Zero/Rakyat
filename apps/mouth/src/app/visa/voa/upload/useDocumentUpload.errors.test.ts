import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useDocumentUpload } from "./useDocumentUpload";
import { messageFor, messageForUnknownCode } from "./messages";

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

describe("useDocumentUpload error states for codes outside the catalog", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    global.fetch = mockFetch;
    mockFetch.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function failWith(status: number, body: unknown) {
    mockFetch.mockResolvedValueOnce(jsonResponse(status, body));
    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    await waitFor(() => expect(result.current.state.step).toBe("error"));
    const state = result.current.state;
    if (state.step !== "error") throw new Error("expected the error step");
    return state;
  }

  it("shows a non-empty, retryable error for a 429 whose code is not listed", async () => {
    const state = await failWith(429, {
      code: "RATE_LIMITED",
      retryable: true,
      message_key: "garuda_voa.error.rate_limited",
    });
    expect(typeof state.message).toBe("string");
    expect(state.message.length).toBeGreaterThan(0);
    expect(state.retryable).toBe(true);
    expect(state.code).toBeNull();
  });

  it("falls back to the HTTP status when the body's retryable flag is not a boolean", async () => {
    const tooMany = await failWith(429, {
      code: "SOMETHING_NEW",
      retryable: "yes",
      message_key: "x.y",
    });
    expect(tooMany.retryable).toBe(true);

    mockFetch.mockReset();
    const serverSide = await failWith(502, {
      code: "SOMETHING_NEW",
      retryable: null,
      message_key: "x.y",
    });
    expect(serverSide.retryable).toBe(true);

    mockFetch.mockReset();
    const clientSide = await failWith(418, {
      code: "SOMETHING_NEW",
      retryable: null,
      message_key: "x.y",
    });
    expect(clientSide.retryable).toBe(false);
  });

  it("honours an explicit false from the server even on an unlisted code", async () => {
    const state = await failWith(429, {
      code: "SOMETHING_NEW",
      retryable: false,
      message_key: "x.y",
    });
    expect(state.retryable).toBe(false);
    expect(state.code).toBeNull();
  });

  it("keeps the catalog code and its exact text for a listed code", async () => {
    const state = await failWith(413, {
      code: "DOCUMENT_TOO_LARGE",
      retryable: false,
      message_key: "garuda_voa.error.document_too_large",
    });
    expect(state.code).toBe("DOCUMENT_TOO_LARGE");
    expect(state.message).toBe(messageFor("DOCUMENT_TOO_LARGE"));
    expect(state.retryable).toBe(false);
  });

  it("still gives the network sentence when the request never reached the server", async () => {
    mockFetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    await waitFor(() => expect(result.current.state.step).toBe("error"));
    const state = result.current.state;
    if (state.step !== "error") throw new Error("expected the error step");
    expect(state.message).toBe(messageForUnknownCode("__network__"));
    expect(state.retryable).toBe(true);
    expect(state.code).toBeNull();
  });
});
