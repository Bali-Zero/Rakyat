import React from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NewSecondHomeCasePage from "./page";

const { mockMutateAsync, mockGetProfile, mockPush } = vi.hoisted(() => ({
  mockMutateAsync: vi.fn(),
  mockGetProfile: vi.fn(),
  mockPush: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));
vi.mock("@/lib/api", () => ({
  api: {
    getProfile: mockGetProfile,
    crm: { getClientPractices: vi.fn().mockResolvedValue([]) },
  },
}));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/hooks/useSecondHome", () => ({
  useCreateSecondHomeCase: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
  }),
}));
vi.mock("../components/ClientPicker", () => ({
  ClientPicker: ({
    onSelect,
  }: {
    onSelect: (c: { id: number; full_name: string }) => void;
  }) => (
    <button
      type="button"
      onClick={() => onSelect({ id: 7, full_name: "Synthetic Client" })}
    >
      pick-client
    </button>
  ),
}));

describe("new Second Home case page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetProfile.mockResolvedValue({});
    mockMutateAsync.mockResolvedValue({ case_id: "E33-2026-synthetic" });
  });

  it("states the catalogue facts for E33F: income or allowance, USD 3,000, 1 year, sponsor", () => {
    render(<NewSecondHomeCasePage />);
    const panel = screen.getByTestId("e33-facts-panel").textContent ?? "";
    expect(panel).toMatch(/income or allowance/i);
    expect(panel).toContain("USD 3,000");
    expect(panel).toMatch(/1 year/i);
    expect(panel).toMatch(/sponsor \(penjamin\) is required/i);
    expect(panel).not.toMatch(/passive\s+income/i);
    // prices come from the catalogue snapshot, in its own format
    expect(panel).toMatch(/\d{2}\.\d{3}\.\d{3} IDR/);
  });

  it("still states the base E33 / E33E facts staff rely on", () => {
    render(<NewSecondHomeCasePage />);
    const panel = screen.getByTestId("e33-facts-panel").textContent ?? "";
    for (const fact of [
      "strata-title",
      "No sponsor",
      "5 years",
      "Pasal 113",
      "55–59",
      "BUMN",
      "USD 130,000",
      "USD 1,000,000",
    ]) {
      expect(panel).toContain(fact);
    }
  });

  it("the page source carries no IDR literal and no passive-income phrase", () => {
    const src = readFileSync(join(__dirname, "page.tsx"), "utf8");
    expect(src).not.toMatch(/\bIDR\s*[0-9]/i);
    expect(src).not.toMatch(/passive\s+income/i);
  });

  it("posts no owner_email when the owner is blank", async () => {
    const user = userEvent.setup();
    render(<NewSecondHomeCasePage />);
    await user.click(screen.getByText("pick-client"));
    await user.click(screen.getByRole("button", { name: /create case/i }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    const body = mockMutateAsync.mock.calls[0][0] as Record<string, unknown>;
    expect(JSON.stringify(body)).not.toContain("owner_email");
    expect(body.client_id).toBe(7);
  });

  it("posts the owner_email when one is typed", async () => {
    const user = userEvent.setup();
    render(<NewSecondHomeCasePage />);
    await user.click(screen.getByText("pick-client"));
    await user.type(
      screen.getByPlaceholderText("team-member@balizero.com"),
      "owner@example.test",
    );
    await user.click(screen.getByRole("button", { name: /create case/i }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    expect(mockMutateAsync.mock.calls[0][0]).toMatchObject({
      owner_email: "owner@example.test",
    });
  });
});
