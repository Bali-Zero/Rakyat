import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { ClientDocument, ClientProfile } from "@/lib/api/crm/crm.types";
import { VisaCard } from "./VisaCard";

vi.mock("@/lib/api", () => ({ api: { post: vi.fn() } }));
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  },
}));
const pollOcrStatus = vi.fn();
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus }),
}));

describe("VisaCard upload contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.post).mockResolvedValue({ success: true });
  });

  it("keeps the Actual Visa routing value when the visible heading says Visa", async () => {
    render(
      <VisaCard
        client={
          { id: 42, full_name: "Sample Client" } as ClientProfile["client"]
        }
        clientId={42}
        documents={[]}
        activePractices={[]}
        formatDate={(v) => v}
        formatCurrency={String}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { name: "Visa" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Upload visa document"), {
      target: {
        files: [
          new File(["synthetic document"], "sample.pdf", {
            type: "application/pdf",
          }),
        ],
      },
    });
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/api/crm/clients/42/documents/upload",
        expect.objectContaining({
          document_type: "visa",
          subfolder_hint: "Actual Visa",
          document_category: "immigration",
          file_name: "sample.pdf",
          mime_type: "application/pdf",
        }),
      ),
    );
    expect(pollOcrStatus).toHaveBeenCalledOnce();
  });

  describe("OCR is explicit", () => {
    const withFile = (over: Record<string, unknown>) =>
      [
        {
          id: 9,
          document_type: "kitas",
          document_category: "immigration",
          file_name: "kitas.pdf",
          google_drive_file_url:
            "https://drive.google.com/file/d/drive-visa-1/view",
          created_at: "2026-10-01T00:00:00Z",
          ...over,
        },
      ] as unknown as ClientDocument[];

    const renderCard = (documents: ClientDocument[]) =>
      render(
        <VisaCard
          client={
            { id: 42, full_name: "Sample Client" } as ClientProfile["client"]
          }
          clientId={42}
          documents={documents}
          activePractices={[]}
          formatDate={(v) => v}
          formatCurrency={String}
          onRefresh={vi.fn().mockResolvedValue(undefined)}
        />,
      );

    const extractCalls = () =>
      vi
        .mocked(api.post)
        .mock.calls.filter((c) => String(c[0]).includes("extract-visa"));

    it("GUILT: mounting with a file and no expiry sends nothing to OCR", async () => {
      renderCard(withFile({ expiry_date: null }));
      await new Promise((r) => setTimeout(r, 50));
      expect(extractCalls()).toHaveLength(0);
      expect(api.post).not.toHaveBeenCalled();
    });

    it("the explicit Extract dates action makes exactly one request, for the newest visa document", async () => {
      vi.mocked(api.post).mockResolvedValue({ success: false });
      renderCard(withFile({ expiry_date: null }));
      const b = screen.getByRole("button", {
        name: "Extract dates from the visa scan",
      });
      expect(b.className).toContain("min-h-8");
      fireEvent.click(b);
      await waitFor(() => expect(extractCalls()).toHaveLength(1));
      expect(extractCalls()[0]).toEqual([
        "/api/crm/clients/42/extract-visa",
        { file_id: "drive-visa-1", doc_id: 9 },
      ]);
    });

    it("INNOCENCE: the action is hidden when an expiry exists, and with no file", () => {
      const { unmount } = renderCard(withFile({ expiry_date: "2027-01-02" }));
      expect(screen.queryByTestId("visa-extract-dates")).toBeNull();
      unmount();
      renderCard(withFile({ expiry_date: null, google_drive_file_url: null }));
      expect(screen.queryByTestId("visa-extract-dates")).toBeNull();
    });
  });
});
