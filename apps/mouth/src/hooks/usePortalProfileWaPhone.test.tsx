import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getProfile } = vi.hoisted(() => ({ getProfile: vi.fn() }));

vi.mock("@/lib/api", () => ({ api: { portal: { getProfile } } }));

import { usePortalProfileWaPhone } from "./usePortalProfileWaPhone";

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

describe("usePortalProfileWaPhone", () => {
  beforeEach(() => {
    getProfile.mockReset();
  });

  it("prefers the profile WhatsApp number, normalised", async () => {
    getProfile.mockResolvedValue({
      whatsapp: "+62 890-0000-0000",
      phone: "+62 890-0000-0001",
    });
    const { result } = renderHook(() => usePortalProfileWaPhone(), {
      wrapper,
    });
    await waitFor(() => expect(result.current).toBe("6289000000000"));
  });

  it("falls back to the profile phone when WhatsApp is empty", async () => {
    getProfile.mockResolvedValue({ whatsapp: "", phone: "0890 0000 0001" });
    const { result } = renderHook(() => usePortalProfileWaPhone(), {
      wrapper,
    });
    await waitFor(() => expect(result.current).toBe("6289000000001"));
  });

  it("returns null when the profile cannot be read", async () => {
    getProfile.mockRejectedValue(new Error("HTTP 500"));
    const { result } = renderHook(() => usePortalProfileWaPhone(), {
      wrapper,
    });
    await waitFor(() => expect(getProfile).toHaveBeenCalled());
    expect(result.current).toBeNull();
  });
});
