import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockRequest, profileWaPhone } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  profileWaPhone: vi.fn<() => string | null>(),
}));

vi.mock("@/lib/api", () => ({ api: { request: mockRequest } }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), info: vi.fn(), warn: vi.fn() },
}));
vi.mock("@/hooks/usePortalProfileWaPhone", () => ({
  usePortalProfileWaPhone: () => profileWaPhone(),
}));

import NotificationsSettingsPage from "./page";

async function renderWithPrefs(wa_phone: string | null) {
  mockRequest.mockImplementation((_url: string, init?: RequestInit) =>
    Promise.resolve(
      init?.method === "PUT"
        ? JSON.parse(String(init.body))
        : { email_enabled: true, wa_enabled: true, wa_phone },
    ),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <NotificationsSettingsPage />
    </QueryClientProvider>,
  );
  await screen.findByText("Email");
}

function putBodies(): unknown[] {
  return mockRequest.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

describe("NotificationsSettingsPage — WhatsApp number from one source", () => {
  beforeEach(() => {
    mockRequest.mockReset();
    profileWaPhone.mockReset();
    profileWaPhone.mockReturnValue(null);
  });

  it("accepts '+62 890-0000-0000' and sends digits only", async () => {
    await renderWithPrefs(null);

    fireEvent.change(screen.getByLabelText(/WhatsApp number/), {
      target: { value: "+62 890-0000-0000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(putBodies()).toEqual([
        { email_enabled: true, wa_enabled: true, wa_phone: "6289000000000" },
      ]),
    );
  });

  it("still refuses a number that is not a phone number", async () => {
    await renderWithPrefs(null);

    fireEvent.change(screen.getByLabelText(/WhatsApp number/), {
      target: { value: "62notaphone" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(/valid number with country code/i),
    ).toBeInTheDocument();
    expect(putBodies()).toEqual([]);
  });

  it("pre-fills the WhatsApp number from the profile when none is saved", async () => {
    profileWaPhone.mockReturnValue("6289000000000");
    await renderWithPrefs(null);

    expect(screen.getByLabelText(/WhatsApp number/)).toHaveValue(
      "6289000000000",
    );
  });

  it("does not overwrite a saved WhatsApp number with the profile one", async () => {
    profileWaPhone.mockReturnValue("6289000000000");
    await renderWithPrefs("6289000000001");

    expect(screen.getByLabelText(/WhatsApp number/)).toHaveValue(
      "6289000000001",
    );
  });
});
