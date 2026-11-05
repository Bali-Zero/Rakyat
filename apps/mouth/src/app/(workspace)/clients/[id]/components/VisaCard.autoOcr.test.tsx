import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { ClientDocument, ClientProfile } from "@/lib/api/crm/crm.types";
import { VisaCard } from "./VisaCard";

vi.mock("@/lib/api", () => ({ api: { post: vi.fn(), delete: vi.fn() } }));
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  },
}));
vi.mock("@/hooks/useOcrPolling", () => ({
  useOcrPolling: () => ({ ocrPolling: false, pollOcrStatus: vi.fn() }),
}));

const visaDoc = (over: Record<string, unknown> = {}) =>
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
      client={{ id: 42, full_name: "Sample Client" } as ClientProfile["client"]}
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
    .mock.calls.filter(([url]) => String(url).endsWith("/extract-visa"));

describe("VisaCard OCR runs only on an explicit click", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.post).mockResolvedValue({ success: true });
  });

  it("GUILT: mounting with a visa file and no expiry sends nothing to OCR", async () => {
    renderCard(visaDoc());
    await new Promise((r) => setTimeout(r, 50));
    expect(extractCalls()).toHaveLength(0);
  });

  it("the Extract button sends exactly one request for the current visa document", async () => {
    renderCard(visaDoc());
    fireEvent.click(screen.getByRole("button", { name: /extract/i }));
    await waitFor(() => expect(extractCalls()).toHaveLength(1));
    expect(extractCalls()[0]).toEqual([
      "/api/crm/clients/42/extract-visa",
      { file_id: "drive-visa-1", doc_id: 9 },
    ]);
  });
});

describe("VisaCard download navigates only safe URLs", () => {
  const clickSpy = () =>
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

  it("GUILT: a javascript: file URL is never clicked", () => {
    const spy = clickSpy();
    renderCard(visaDoc({ google_drive_file_url: "javascript:alert(1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Download visa" }));
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("INNOCENCE: an https non-Drive-ID URL still downloads", () => {
    const spy = clickSpy();
    renderCard(
      visaDoc({
        google_drive_file_url: "https://files.example.test/kitas.pdf",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Download visa" }));
    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });
});

describe("VisaCard empty state with only a rejected permit", () => {
  it("GUILT: a lone rejected permit reads 'Only a rejected permit on file'", () => {
    renderCard(visaDoc({ status: "rejected", expiry_date: "2030-01-01" }));
    expect(screen.getByText(/Only a rejected permit on file/)).toBeTruthy();
    expect(screen.queryByText(/No visa or KITAS on file/)).toBeNull();
  });

  it("INNOCENCE: no documents at all keeps 'No visa or KITAS on file'", () => {
    renderCard([]);
    expect(screen.getByText(/No visa or KITAS on file/)).toBeTruthy();
    expect(screen.queryByText(/rejected permit/)).toBeNull();
  });
});
