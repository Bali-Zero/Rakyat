import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";
import { useProcessTimeline } from "./useProcessTimeline";
import { ApiError } from "@/lib/api/error-handler";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));

import { api } from "@/lib/api";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig
    value={{
      provider: () => new Map(),
      dedupingInterval: 0,
      errorRetryInterval: 10,
      errorRetryCount: 3,
    }}
  >
    {children}
  </SWRConfig>
);

describe("useProcessTimeline retry rules read the ApiError status", () => {
  beforeEach(() => vi.clearAllMocks());

  it("GUILT: a server error (503) is retried", async () => {
    vi.mocked(api.get).mockRejectedValue(new ApiError("down", 503));
    renderHook(() => useProcessTimeline("42"), { wrapper });
    await waitFor(() =>
      expect(vi.mocked(api.get).mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("INNOCENCE: a client error (404) is not retried", async () => {
    vi.mocked(api.get).mockRejectedValue(new ApiError("nope", 404));
    const { result } = renderHook(() => useProcessTimeline("42"), { wrapper });
    await waitFor(() => expect(result.current.error).toBeDefined());
    await new Promise((r) => setTimeout(r, 300));
    expect(vi.mocked(api.get)).toHaveBeenCalledTimes(1);
  });

  it("ApiError exposes the status under both names", () => {
    const err = new ApiError("x", 502);
    expect(err.status).toBe(502);
    expect(err.statusCode).toBe(502);
  });
});
