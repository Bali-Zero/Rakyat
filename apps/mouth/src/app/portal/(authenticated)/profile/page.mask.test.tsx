import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockGetProfile, mockReveal, mockToastError } = vi.hoisted(() => ({
  mockGetProfile: vi.fn(),
  mockReveal: vi.fn(),
  mockToastError: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    portal: {
      getProfile: mockGetProfile,
      updateProfile: vi.fn(),
      revealProfileIdentity: mockReveal,
    },
  },
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: mockToastError }),
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

// DAI-140: the API now sends both fields masked by default; the full values
// come only from POST /api/portal/profile/reveal on the first Show.
const MASKED_PROFILE: PortalProfile = {
  ...PROFILE,
  passportNumber: "•••••123",
  dateOfBirth: "1990",
  identityMasked: true,
  birthdayToday: false,
};

describe("ProfilePage identity reveal via API (guard, DAI-140)", () => {
  beforeEach(() => {
    mockReveal.mockReset();
    mockToastError.mockReset();
  });

  async function renderMasked() {
    mockGetProfile.mockResolvedValue(MASKED_PROFILE);
    const utils = render(<ProfilePage />);
    await screen.findByText("Test Client");
    return utils;
  }

  it("does not call reveal until the client presses Show", async () => {
    const { container } = await renderMasked();
    expect(mockReveal).not.toHaveBeenCalled();
    expect(screen.getByText("•••••123")).toBeInTheDocument();
    expect(screen.getByText(/•• ••• 1990/)).toBeInTheDocument();
    expect(container.innerHTML).not.toContain(PASSPORT);
  });

  it("Show fetches the full values once and displays them", async () => {
    mockReveal.mockResolvedValue({
      passportNumber: PASSPORT,
      dateOfBirth: DOB,
    });
    const { container } = await renderMasked();

    fireEvent.click(
      screen.getByRole("button", { name: "Show passport number" }),
    );
    expect(await screen.findByText(PASSPORT)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show date of birth" }));
    await waitFor(() => expect(container.innerHTML).toContain(formatDate(DOB)));
    expect(mockReveal).toHaveBeenCalledTimes(1);
  });

  it("stays masked and tells the client when reveal fails", async () => {
    mockReveal.mockRejectedValue(new Error("HTTP 403"));
    const { container } = await renderMasked();

    fireEvent.click(
      screen.getByRole("button", { name: "Show passport number" }),
    );
    await waitFor(() => expect(mockToastError).toHaveBeenCalled());
    expect(container.innerHTML).not.toContain(PASSPORT);
    expect(
      screen.getByRole("button", { name: "Show passport number" }),
    ).toBeInTheDocument();
  });
});
