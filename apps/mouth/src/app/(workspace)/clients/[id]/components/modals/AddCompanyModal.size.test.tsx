/**
 * Guard — Add Company sends NIB/NPWP/AKTA/SK files as base64 in JSON through
 * the Next proxy (~4.5 MB body limit), for OCR and for the document upload
 * after save. A file above the shared base64 cap must be refused when picked,
 * not fail later with a generic "Extraction failed" or a silent upload error.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AddCompanyModal } from "./AddCompanyModal";

const { toastError, apiPost } = vi.hoisted(() => ({
  toastError: vi.fn(),
  apiPost: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    post: apiPost,
    crm: { createCompany: vi.fn(), linkClientToCompany: vi.fn() },
  },
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: toastError, warning: vi.fn() },
}));

function nibInput(): HTMLInputElement {
  return document.getElementById("nib-doc-upload") as HTMLInputElement;
}

describe("AddCompanyModal — base64 size cap (19 Oct)", () => {
  beforeEach(() => {
    toastError.mockReset();
    apiPost.mockReset();
  });

  it("GUILT: a 3.5 MB NIB scan is refused with the 3 MB message and not kept", async () => {
    render(
      <AddCompanyModal clientId={1} onClose={vi.fn()} onSuccess={vi.fn()} />,
    );
    const big = new File([new Uint8Array(3.5 * 1024 * 1024)], "nib.pdf", {
      type: "application/pdf",
    });
    await userEvent.upload(nibInput(), big);
    expect(toastError).toHaveBeenCalledWith("File too large", {
      description: "Maximum file size is 3 MB",
    });
    expect(screen.getByText("Upload NIB file")).toBeInTheDocument();
    expect(screen.queryByText("nib.pdf")).toBeNull();
  });

  it("INNOCENCE: a 1 MB NIB scan is kept", async () => {
    render(
      <AddCompanyModal clientId={1} onClose={vi.fn()} onSuccess={vi.fn()} />,
    );
    const ok = new File([new Uint8Array(1024 * 1024)], "nib-ok.pdf", {
      type: "application/pdf",
    });
    await userEvent.upload(nibInput(), ok);
    expect(toastError).not.toHaveBeenCalled();
    expect(screen.getByText("nib-ok.pdf")).toBeInTheDocument();
  });
});
