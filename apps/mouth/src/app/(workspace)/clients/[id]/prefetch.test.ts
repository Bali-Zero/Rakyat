import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getClientProfile, getPortalStatus } = vi.hoisted(() => ({
  getClientProfile: vi.fn(),
  getPortalStatus: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { crm: { getClientProfile, getPortalStatus } },
}));

import { clientDetailQueryKey } from "@/hooks/useClientDetail";
import { earlyKey, resetEarly, takeEarly } from "./components/early-fetch";
import { clientIdFromPath, prefetchClientDetail } from "./prefetch";

describe("clientIdFromPath", () => {
  it("reads the id of a client detail path only", () => {
    expect(clientIdFromPath("/clients/12714")).toBe(12714);
    expect(clientIdFromPath("/clients/12714/")).toBe(12714);
    expect(clientIdFromPath("/clients")).toBeNull();
    expect(clientIdFromPath("/clients/new")).toBeNull();
    expect(clientIdFromPath("/clients/0")).toBeNull();
    expect(clientIdFromPath("/clients/12/edit")).toBeNull();
    expect(clientIdFromPath("/dashboard")).toBeNull();
    expect(clientIdFromPath(null)).toBeNull();
  });
});

describe("prefetchClientDetail", () => {
  beforeEach(() => {
    resetEarly();
    getClientProfile.mockReset().mockResolvedValue({ client: { id: 7 } });
    getPortalStatus.mockReset().mockResolvedValue({ has_access: false });
  });
  afterEach(() => resetEarly());

  it("starts profile and portal status together, and the page reuses both", async () => {
    const qc = new QueryClient();
    prefetchClientDetail(qc, 7);

    // Both requests are in flight before anything awaits.
    expect(getClientProfile).toHaveBeenCalledWith(7);
    expect(getPortalStatus).toHaveBeenCalledWith(7);

    // The page's useQuery reads the same key — no second profile request.
    await vi.waitFor(() =>
      expect(qc.getQueryData(clientDetailQueryKey(7))).toEqual({
        client: { id: 7 },
      }),
    );
    // PortalAccess takes the early promise once.
    await expect(takeEarly(earlyKey("portal-status", 7))).resolves.toEqual({
      has_access: false,
    });
  });

  it("does not fire twice when called again (layout + page mount)", () => {
    const qc = new QueryClient();
    prefetchClientDetail(qc, 7);
    prefetchClientDetail(qc, 7);
    expect(getClientProfile).toHaveBeenCalledTimes(1);
    expect(getPortalStatus).toHaveBeenCalledTimes(1);
  });

  it("ignores a non-positive id", () => {
    prefetchClientDetail(new QueryClient(), 0);
    expect(getClientProfile).not.toHaveBeenCalled();
    expect(getPortalStatus).not.toHaveBeenCalled();
  });
});
