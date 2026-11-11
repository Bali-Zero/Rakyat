import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getCapabilitiesMock } = vi.hoisted(() => ({
  getCapabilitiesMock: vi.fn(),
}));

vi.mock("@/lib/api/hr/hr", () => ({
  getCapabilities: getCapabilitiesMock,
}));

import { NO_CAPABILITIES, useHrCapabilities } from "./use-hr-capabilities";

describe("useHrCapabilities", () => {
  beforeEach(() => {
    getCapabilitiesMock.mockReset();
  });

  it("grants nothing before the server answers", () => {
    getCapabilitiesMock.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useHrCapabilities());
    expect(result.current).toEqual({
      capabilities: {
        hr_admin: false,
        accounting_admin: false,
        has_employee_record: false,
      },
      loaded: false,
      failed: false,
    });
  });

  it("returns the server's answer as is", async () => {
    const answer = {
      hr_admin: true,
      accounting_admin: false,
      has_employee_record: true,
    };
    getCapabilitiesMock.mockResolvedValue(answer);
    const { result } = renderHook(() => useHrCapabilities());
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.capabilities).toEqual(answer);
  });

  it("falls to the member view when the read fails", async () => {
    getCapabilitiesMock.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useHrCapabilities());
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.capabilities).toEqual(NO_CAPABILITIES);
    expect(result.current.failed).toBe(true);
  });
});
