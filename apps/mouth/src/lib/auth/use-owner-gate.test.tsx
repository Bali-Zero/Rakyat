import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getProfileMock, replaceMock } = vi.hoisted(() => ({
  getProfileMock: vi.fn(),
  replaceMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

vi.mock("@/lib/api", () => ({
  api: { getProfile: getProfileMock },
}));

import { useOwnerGate } from "./use-owner-gate";

describe("useOwnerGate", () => {
  beforeEach(() => {
    getProfileMock.mockReset();
    replaceMock.mockReset();
  });

  it("lets the owner through and does not redirect", async () => {
    getProfileMock.mockResolvedValue({ email: "zero@balizero.com" });
    const { result } = renderHook(() => useOwnerGate());
    expect(result.current).toBe("loading");
    await waitFor(() => expect(result.current).toBe("owner"));
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("sends a global admin who is not the owner back to /hr", async () => {
    getProfileMock.mockResolvedValue({ email: "subhi@balizero.com" });
    const { result } = renderHook(() => useOwnerGate());
    await waitFor(() => expect(result.current).toBe("forbidden"));
    expect(replaceMock).toHaveBeenCalledWith("/hr");
  });

  it("treats a failed profile read as not-the-owner", async () => {
    getProfileMock.mockRejectedValue(new Error("401"));
    const { result } = renderHook(() => useOwnerGate("/hr"));
    await waitFor(() => expect(result.current).toBe("forbidden"));
    expect(replaceMock).toHaveBeenCalledWith("/hr");
  });
});
