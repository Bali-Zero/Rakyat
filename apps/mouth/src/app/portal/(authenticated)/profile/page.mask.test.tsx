import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const { mockGetProfile } = vi.hoisted(() => ({ mockGetProfile: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { portal: { getProfile: mockGetProfile, updateProfile: vi.fn() } },
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: vi.fn() }),
}));

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn() },
}));

import type { PortalProfile } from "@/lib/api/portal/portal.types";
import { formatDate } from "@/lib/utils/format-date";
import ProfilePage from "./page";

// Synthetic identity only — no real client data.
const PASSPORT = "X0000123";
const DOB = "1990-02-14";

const PROFILE: PortalProfile = {
  id: 1,
  fullName: "Test Client",
  email: "client@example.test",
  nationality: "Testland",
  passportNumber: PASSPORT,
  passportExpiry: new Date(Date.now() + 900 * 86400000).toISOString(),
  dateOfBirth: DOB,
  memberSince: "2024-01-01T00:00:00Z",
} as PortalProfile;

async function renderLoaded() {
  mockGetProfile.mockResolvedValue(PROFILE);
  const utils = render(<ProfilePage />);
  await screen.findByText("Test Client");
  return utils;
}

describe("ProfilePage identity masking (guard)", () => {
  it("first render shows neither the full passport number nor the full date of birth", async () => {
    const { container } = await renderLoaded();
    const html = container.innerHTML;

    expect(html).not.toContain(PASSPORT);
    expect(html).not.toContain(formatDate(DOB));
    // Masked forms keep just enough to recognise the document.
    expect(screen.getByText("•••••123")).toBeInTheDocument();
    expect(screen.getByText(/•• ••• 1990/)).toBeInTheDocument();
  });

  it("Show reveals each value, Hide masks it again", async () => {
    const { container } = await renderLoaded();

    fireEvent.click(
      screen.getByRole("button", { name: "Show passport number" }),
    );
    expect(screen.getByText(PASSPORT)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show date of birth" }));
    expect(container.innerHTML).toContain(formatDate(DOB));

    fireEvent.click(
      screen.getByRole("button", { name: "Hide passport number" }),
    );
    expect(container.innerHTML).not.toContain(PASSPORT);
  });

  it("is masked again on every fresh load", async () => {
    const first = await renderLoaded();
    fireEvent.click(
      screen.getByRole("button", { name: "Show passport number" }),
    );
    first.unmount();

    const { container } = await renderLoaded();
    expect(container.innerHTML).not.toContain(PASSPORT);
  });
});
