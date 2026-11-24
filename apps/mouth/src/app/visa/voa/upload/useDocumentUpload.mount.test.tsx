import { useEffect } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  act,
  configure,
  getConfig,
  renderHook,
  waitFor,
} from "@testing-library/react";
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

const READY_BODY = {
  document_id: "doc-1",
  processing_state: "READY_FOR_REVIEW",
  review_fields: [
    { field_path: "full_name", value: "JANE DOE", confirmation_required: true },
  ],
};

describe("useDocumentUpload mount lifecycle", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    global.fetch = mockFetch;
    mockFetch.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reaches ready when the component mounts, cleans up and mounts again (StrictMode)", async () => {
    const previous = getConfig().reactStrictMode;
    configure({ reactStrictMode: true });
    try {
      mockFetch.mockResolvedValueOnce(jsonResponse(201, READY_BODY));
      const effectLog: string[] = [];

      const { result } = renderHook(() => {
        useEffect(() => {
          effectLog.push("mount");
          return () => {
            effectLog.push("cleanup");
          };
        }, []);
        return useDocumentUpload("result-1");
      });
      // Guards the test itself: it only means something if React really ran the
      // mount, cleanup, mount sequence.
      expect(effectLog).toEqual(["mount", "cleanup", "mount"]);

      act(() => {
        result.current.selectFile(makeFile());
      });

      await waitFor(() => expect(result.current.state.step).toBe("ready"));
    } finally {
      configure({ reactStrictMode: previous });
    }
  });

  it("still reaches ready outside StrictMode", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(201, READY_BODY));
    const { result } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    await waitFor(() => expect(result.current.state.step).toBe("ready"));
  });

  it("drops a late answer after a real unmount, without a warning", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let resolveFetch!: (r: Response) => void;
    mockFetch.mockImplementationOnce(
      () =>
        new Promise<Response>((res) => {
          resolveFetch = res;
        }),
    );

    const { result, unmount } = renderHook(() => useDocumentUpload("result-1"));
    act(() => {
      result.current.selectFile(makeFile());
    });
    expect(result.current.state.step).toBe("uploading");

    unmount();
    await act(async () => {
      resolveFetch(jsonResponse(201, READY_BODY));
      await Promise.resolve();
    });

    expect(result.current.state.step).toBe("uploading");
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
