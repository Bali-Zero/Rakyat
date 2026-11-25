import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const prefsHook = vi.fn();
const profileWaPhone = vi.fn<() => string | null>();

vi.mock("@/hooks/useNotificationPrefs", () => ({
  useNotificationPrefs: () => prefsHook(),
}));
vi.mock("@/hooks/usePortalProfileWaPhone", () => ({
  usePortalProfileWaPhone: () => profileWaPhone(),
}));

import { NotificationSettings } from "./NotificationSettings";

function mockPrefs(wa_phone: string | null) {
  const updatePrefs = vi.fn().mockResolvedValue(null);
  prefsHook.mockReturnValue({
    data: { email_enabled: true, wa_enabled: false, wa_phone },
    migrationMissing: false,
    isLoading: false,
    error: undefined,
    mutate: vi.fn(),
    updatePrefs,
    isUpdating: false,
  });
  return updatePrefs;
}

describe("NotificationSettings — WhatsApp number from one source", () => {
  beforeEach(() => {
    prefsHook.mockReset();
    profileWaPhone.mockReset();
    profileWaPhone.mockReturnValue(null);
  });

  it("accepts '+62 890-0000-0000' and sends digits only", () => {
    const updatePrefs = mockPrefs(null);
    render(<NotificationSettings />);

    const phone = screen.getByLabelText(/whatsapp number/i);
    fireEvent.change(phone, { target: { value: "+62 890-0000-0000" } });
    fireEvent.blur(phone);
    expect(updatePrefs).toHaveBeenNthCalledWith(1, {
      email_enabled: true,
      wa_enabled: false,
      wa_phone: "6289000000000",
    });

    fireEvent.click(screen.getByLabelText(/whatsapp notifications/i));
    expect(updatePrefs).toHaveBeenNthCalledWith(2, {
      email_enabled: true,
      wa_enabled: true,
      wa_phone: "6289000000000",
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still refuses a number that is not a phone number", () => {
    const updatePrefs = mockPrefs(null);
    render(<NotificationSettings />);

    const phone = screen.getByLabelText(/whatsapp number/i);
    fireEvent.change(phone, { target: { value: "62notaphone" } });
    fireEvent.blur(phone);
    expect(updatePrefs).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /valid whatsapp number/i,
    );
  });

  it("pre-fills the WhatsApp number from the profile when none is saved", () => {
    profileWaPhone.mockReturnValue("6289000000000");
    mockPrefs(null);
    render(<NotificationSettings />);

    expect(screen.getByLabelText(/whatsapp number/i)).toHaveValue(
      "6289000000000",
    );
  });

  it("does not overwrite a saved WhatsApp number with the profile one", () => {
    profileWaPhone.mockReturnValue("6289000000000");
    mockPrefs("6289000000001");
    render(<NotificationSettings />);

    expect(screen.getByLabelText(/whatsapp number/i)).toHaveValue(
      "6289000000001",
    );
  });
});
