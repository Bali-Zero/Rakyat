import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const api = vi.hoisted(() => ({
  getPartner: vi.fn(),
  updatePartner: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const push = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/partners/partners", () => api);
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "partner-1" }),
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

import EditPartnerPage from "./page";

const SECTIONS = ["Profile", "Fiscal", "Payment", "Commission"];

function partner(overrides: Record<string, unknown> = {}) {
  return {
    id: "partner-1",
    full_name: "Zz Partner",
    email: "partner@example.test",
    entity_type: "individual",
    tax_withholding_category: "tbd",
    default_commission_type: "percentage",
    default_commission_value: 10,
    onboarding_status: "active",
    phone: "+62 811 000 000",
    company_name: "Zz Agency",
    work_role: "Agent",
    npwp: null,
    bank_name: "Bank Lama",
    bank_account_number: "1234567890",
    bank_account_holder: "Zz Holder",
    ...overrides,
  };
}

/** The control under a label of the form (the labels are not tied to their inputs). */
function field(label: string): HTMLInputElement {
  const found = screen.getByText(label, { selector: "label" });
  return found.parentElement!.querySelector(
    "input, textarea, select",
  ) as HTMLInputElement;
}

function open(section: string) {
  fireEvent.click(screen.getByRole("button", { name: section }));
}

async function saved(): Promise<Record<string, unknown>> {
  fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));
  await waitFor(() => expect(api.updatePartner).toHaveBeenCalledTimes(1));
  expect(api.updatePartner.mock.calls[0][0]).toBe("partner-1");
  return api.updatePartner.mock.calls[0][1];
}

describe("EditPartnerPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getPartner.mockResolvedValue(partner());
    api.updatePartner.mockResolvedValue(partner());
  });

  it("empties the bank account the operator cleared, and keeps the rest", async () => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    open("Payment");
    fireEvent.change(field("Account Number"), { target: { value: "" } });
    const body = await saved();

    expect(body.bank_account_number).toBeNull();
    expect(body.bank_name).toBe("Bank Lama");
    expect(body.bank_account_holder).toBe("Zz Holder");
    expect(toast.success).toHaveBeenCalledWith("Partner updated");
  });

  it.each([
    ["Profile", "Phone", "phone"],
    ["Profile", "Company Name", "company_name"],
    ["Profile", "Work Role", "work_role"],
    ["Payment", "Bank Name", "bank_name"],
    ["Payment", "Account Holder Name", "bank_account_holder"],
  ])("empties %s / %s when it is cleared", async (section, label, key) => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    open(section);
    fireEvent.change(field(label), { target: { value: "   " } });
    const body = await saved();

    expect(body[key]).toBeNull();
  });

  it("sends a field that was empty and stays empty as empty, not as missing", async () => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    const body = await saved();

    expect(body).toHaveProperty("npwp", null);
  });

  it("sends what the operator typed", async () => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    open("Payment");
    fireEvent.change(field("Account Number"), {
      target: { value: " 9876543210 " },
    });
    const body = await saved();

    expect(body.bank_account_number).toBe("9876543210");
  });

  it("sends only what the partner record holds", async () => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    const body = await saved();

    expect(Object.keys(body).sort()).toEqual(
      [
        "bank_account_holder",
        "bank_account_number",
        "bank_name",
        "company_name",
        "default_commission_type",
        "default_commission_value",
        "full_name",
        "npwp",
        "phone",
        "tax_withholding_category",
        "work_role",
      ].sort(),
    );
  });

  it("shows no control for what the partner record cannot hold", async () => {
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    for (const section of SECTIONS) {
      open(section);
      for (const label of [
        "WhatsApp",
        "Nationality",
        "Notes",
        "Payment Method",
      ]) {
        expect(screen.queryByText(label, { selector: "label" })).toBeNull();
      }
    }
  });

  it("says the update failed when the save is refused", async () => {
    api.updatePartner.mockRejectedValue(new Error("422"));
    render(<EditPartnerPage />);
    await screen.findByDisplayValue("Zz Partner");

    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });
});
