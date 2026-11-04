/**
 * Guard — Add Company NPWP validation.
 *
 * The form accepted only 15-digit NPWP, but the 16-digit format is in use
 * (Coretax) and the backend stores either. A 16-digit NPWP blocked the save
 * with a bare "Please fix form errors" toast: field errors were never shown,
 * so staff could not tell which field was wrong.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AddCompanyModal } from "./AddCompanyModal";

const { toastError, createCompany, linkClientToCompany } = vi.hoisted(() => ({
  toastError: vi.fn(),
  createCompany: vi.fn(),
  linkClientToCompany: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { post: vi.fn(), crm: { createCompany, linkClientToCompany } },
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: toastError, warning: vi.fn() },
}));

async function fillAndCreate(npwp: string) {
  render(
    <AddCompanyModal clientId={1} onClose={vi.fn()} onSuccess={vi.fn()} />,
  );
  await userEvent.type(
    screen.getByPlaceholderText("e.g. PT Bali Investment Mandiri"),
    "PT Uji Coba",
  );
  await userEvent.type(screen.getByPlaceholderText("Company Tax ID"), npwp);
  await userEvent.click(
    screen.getByRole("button", { name: /create company/i }),
  );
}

describe("AddCompanyModal — NPWP 15 or 16 digits (19 Oct)", () => {
  beforeEach(() => {
    toastError.mockReset();
    createCompany.mockReset();
    createCompany.mockResolvedValue({ id: 77 });
    linkClientToCompany.mockReset();
    linkClientToCompany.mockResolvedValue({});
  });

  it("GUILT: a 16-digit NPWP is accepted and saved", async () => {
    await fillAndCreate("0123456789012345");
    await waitFor(() => expect(createCompany).toHaveBeenCalled());
    expect(createCompany.mock.calls[0][0]).toMatchObject({
      npwp_company: "0123456789012345",
    });
  });

  it("INNOCENCE: a 15-digit NPWP is still accepted", async () => {
    await fillAndCreate("99.000.000.1-000.002");
    await waitFor(() => expect(createCompany).toHaveBeenCalled());
  });

  it("GUILT: a wrong NPWP says which field is wrong", async () => {
    await fillAndCreate("12345");
    expect(toastError).toHaveBeenCalledWith("Please fix form errors", {
      description: "NPWP must be 15 or 16 digits",
    });
    expect(createCompany).not.toHaveBeenCalled();
  });
});
