import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// The page runs over the real partners client: only the HTTP layer is a
// stand-in, so the address asserted below is the one the server would get.
const api = vi.hoisted(() => ({ get: vi.fn(), isAdmin: () => false }));
const navigation = vi.hoisted(() => ({ search: "" }));

vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [] }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

import PartnersPage from "./page";

function lastQuery(): Record<string, string> {
  const calls = api.get.mock.calls;
  const url = new URL(calls[calls.length - 1][0] as string, "http://kita.test");
  expect(url.pathname).toBe("/api/partners");
  return Object.fromEntries(url.searchParams);
}

describe("the status filter of the partners list", () => {
  beforeEach(() => {
    navigation.search = "";
    api.get.mockReset();
    api.get.mockResolvedValue({
      partners: [],
      total: 0,
      page: 1,
      page_size: 50,
    });
  });

  it("asks the server for the chosen status", async () => {
    render(<PartnersPage />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    expect(lastQuery()).toEqual({ page: "1", page_size: "50" });

    fireEvent.change(screen.getByDisplayValue("All statuses"), {
      target: { value: "pending_approval" },
    });

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(lastQuery()).toEqual({
      onboarding_status: "pending_approval",
      page: "1",
      page_size: "50",
    });
  });

  it("asks for the status named in the page address", async () => {
    navigation.search = "status=inactive";

    render(<PartnersPage />);

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    expect(lastQuery()).toEqual({
      onboarding_status: "inactive",
      page: "1",
      page_size: "50",
    });
  });

  it("goes back to every status", async () => {
    navigation.search = "status=active";
    render(<PartnersPage />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByDisplayValue("Active"), {
      target: { value: "" },
    });

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(lastQuery()).toEqual({ page: "1", page_size: "50" });
  });
});
