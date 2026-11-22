import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const api = vi.hoisted(() => ({ createPartner: vi.fn() }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const push = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/partners/partners", () => api);
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [], isLoading: false }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, back: vi.fn(), replace: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

import NewPartnerPage from "./page";

function field(label: string): HTMLInputElement {
  const found = screen.getByText(label, { selector: "label" });
  return found.parentElement!.querySelector(
    "input, textarea, select",
  ) as HTMLInputElement;
}

function next() {
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
}

describe("NewPartnerPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createPartner.mockResolvedValue({
      id: "partner-1",
      full_name: "Zz Partner",
    });
  });

  it("shows no control for what the partner record cannot hold, on any step", () => {
    render(<NewPartnerPage />);

    const seen: string[] = [];
    for (let step = 0; step < 4; step += 1) {
      for (const label of [
        "WhatsApp",
        "Nationality",
        "Notes",
        "Payment Method",
      ]) {
        expect(screen.queryByText(label, { selector: "label" })).toBeNull();
      }
      if (screen.queryByText("Account Number", { selector: "label" }))
        seen.push("payment");
      if (screen.queryByRole("button", { name: "Next" })) next();
    }
    expect(seen).toEqual(["payment"]);
    expect(
      screen.getByRole("button", { name: "Create Partner" }),
    ).toBeInTheDocument();
  });

  it("creates the partner with the fields the record holds and nothing else", async () => {
    render(<NewPartnerPage />);

    fireEvent.change(field("Full Name *"), { target: { value: "Zz Partner" } });
    fireEvent.change(field("Email *"), {
      target: { value: "partner@example.test" },
    });
    next();
    next();
    fireEvent.change(field("Account Number"), {
      target: { value: "1234567890" },
    });
    next();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create Partner" }));

    await waitFor(() => expect(api.createPartner).toHaveBeenCalledTimes(1));
    const body = api.createPartner.mock.calls[0][0];
    expect(body.full_name).toBe("Zz Partner");
    expect(body.bank_account_number).toBe("1234567890");
    for (const key of ["whatsapp", "nationality", "notes", "payment_method"]) {
      expect(body).not.toHaveProperty(key);
    }
  });
});
