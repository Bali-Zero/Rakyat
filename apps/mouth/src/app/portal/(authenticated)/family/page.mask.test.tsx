import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { mockUseQuery } = vi.hoisted(() => ({ mockUseQuery: vi.fn() }));

vi.mock("@tanstack/react-query", () => ({ useQuery: mockUseQuery }));

vi.mock("@/lib/api", () => ({ api: { request: vi.fn() } }));

import PortalFamilyPage from "./page";

// Synthetic identity only — no real client data.
const ADULT_PASSPORT = "X1234567";
const MINOR_PASSPORT = "X7654321";
const MINOR_DOB = "2018-03-15";

const member = (overrides: Record<string, unknown>) => ({
  date_of_birth: null,
  nationality: null,
  passport_number: null,
  passport_expiry: null,
  visa_type: null,
  visa_expiry: null,
  email: null,
  phone: null,
  ...overrides,
});

const FAMILY = {
  adults: [
    member({
      id: 1,
      full_name: "Ada Example",
      relationship: "Spouse",
      is_adult: true,
      date_of_birth: "1990-05-10",
      passport_number: ADULT_PASSPORT,
    }),
  ],
  minors: [
    member({
      id: 2,
      full_name: "Ben Example",
      relationship: "Child",
      is_adult: false,
      date_of_birth: MINOR_DOB,
      passport_number: MINOR_PASSPORT,
      passport_expiry: "2029-01-01",
    }),
  ],
};

function renderLoaded() {
  mockUseQuery.mockReturnValue({
    data: FAMILY,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  });
  return render(<PortalFamilyPage />);
}

describe("PortalFamilyPage identity masking (guard)", () => {
  it("never renders a family member's full passport number", () => {
    const { container } = renderLoaded();
    const html = container.innerHTML;

    expect(html).not.toContain(ADULT_PASSPORT);
    expect(html).not.toContain(MINOR_PASSPORT);
    // Masked form keeps the last three characters, same as the profile page.
    expect(screen.getByText("PP •••••567")).toBeInTheDocument();
    expect(screen.getByText("PP •••••321")).toBeInTheDocument();
  });

  it("shows only the birth year of a minor, not the full date", () => {
    const { container } = renderLoaded();

    expect(container.innerHTML).not.toContain(
      new Date(MINOR_DOB).toLocaleDateString(),
    );
    expect(screen.getByText("DOB •• ••• 2018")).toBeInTheDocument();
  });

  it("tells the client where the full values live", () => {
    renderLoaded();
    expect(
      screen.getByText(/full passport numbers and dates of birth are in/i),
    ).toBeInTheDocument();
  });

  it("keeps the passport expiry readable (not identity data)", () => {
    const { container } = renderLoaded();
    expect(container.innerHTML).toContain(
      `PP exp ${new Date("2029-01-01").toLocaleDateString()}`,
    );
  });
});
