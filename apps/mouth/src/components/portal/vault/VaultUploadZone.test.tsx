import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useVaultUpload — control its returned state per test.
const mockUpload = vi.fn();
const mockReset = vi.fn();
const mockRetry = vi.fn();
let mockState: { status: string; [k: string]: unknown } = { status: "idle" };
let mockCanRetry = false;

vi.mock("@/hooks/useVaultUpload", () => ({
  useVaultUpload: () => ({
    state: mockState,
    upload: mockUpload,
    reset: mockReset,
    retry: mockRetry,
    canRetry: mockCanRetry,
  }),
}));

const mockShrink = vi.fn();
vi.mock("@/lib/vault/shrinkImage", () => ({
  shrinkImage: (...args: unknown[]) => mockShrink(...args),
}));

import { VaultUploadZone } from "./VaultUploadZone";

function pick(file: File, practiceId?: number) {
  render(<VaultUploadZone practiceId={practiceId} />);
  const input = screen.getByLabelText(
    "Choose file to upload",
  ) as HTMLInputElement;
  Object.defineProperty(input, "files", { value: [file] });
  fireEvent.change(input);
}

function sized(name: string, size: number, type: string): File {
  return new File([new Uint8Array(size)], name, { type });
}

describe("VaultUploadZone — phone photos above the 4 MB platform cap", () => {
  beforeEach(() => {
    mockUpload.mockReset();
    mockShrink.mockReset();
    mockState = { status: "idle" };
  });

  it("re-encodes a 6 MB photo and uploads the smaller file", async () => {
    const smaller = sized("IMG_0001.jpg", 900 * 1024, "image/jpeg");
    mockShrink.mockResolvedValue(smaller);
    pick(sized("IMG_0001.jpg", 6 * 1024 * 1024, "image/jpeg"), 7);

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    expect(mockShrink.mock.calls[0][1]).toBe(4 * 1024 * 1024);
    expect(mockUpload.mock.calls[0][0]).toBe(smaller);
    expect(mockUpload.mock.calls[0][1]).toEqual({
      practiceId: 7,
      purpose: "",
      documentType: "other",
    });
  });

  it("falls back to the original when the photo cannot be shrunk (upload() then refuses it by size)", async () => {
    const big = sized("IMG_0002.png", 6 * 1024 * 1024, "image/png");
    mockShrink.mockResolvedValue(null);
    pick(big);

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    expect(mockUpload.mock.calls[0][0]).toBe(big);
  });

  it("positive control: small files and PDFs are never re-encoded", () => {
    pick(sized("scan.pdf", 6 * 1024 * 1024, "application/pdf"));
    expect(mockShrink).not.toHaveBeenCalled();
    expect(mockUpload).toHaveBeenCalledTimes(1);
  });
});

describe("VaultUploadZone", () => {
  beforeEach(() => {
    mockUpload.mockReset();
    mockReset.mockReset();
    mockRetry.mockReset();
    mockState = { status: "idle" };
    mockCanRetry = false;
  });

  it("toggles drag-over state on drag events", () => {
    render(<VaultUploadZone />);
    const zone = screen.getByTestId("vault-upload-zone");
    expect(zone.getAttribute("data-drag-over")).toBe("false");

    fireEvent.dragOver(zone);
    expect(zone.getAttribute("data-drag-over")).toBe("true");

    fireEvent.dragLeave(zone);
    expect(zone.getAttribute("data-drag-over")).toBe("false");
  });

  it("calls upload() when file input changes", () => {
    render(<VaultUploadZone practiceId={42} />);
    const input = screen.getByLabelText(
      "Choose file to upload",
    ) as HTMLInputElement;
    const file = new File(["x"], "a.pdf", { type: "application/pdf" });
    Object.defineProperty(input, "files", { value: [file] });
    fireEvent.change(input);
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(mockUpload.mock.calls[0][0]).toBe(file);
    // FASE 5: purpose is now part of the upload options (empty when untouched).
    expect(mockUpload.mock.calls[0][1]).toEqual({
      practiceId: 42,
      purpose: "",
      documentType: "other",
    });
  });

  it("advertises exactly the formats and cap enforced by the backend", () => {
    render(<VaultUploadZone />);
    const input = screen.getByLabelText(
      "Choose file to upload",
    ) as HTMLInputElement;
    expect(input).toHaveAttribute("accept", ".pdf,.jpg,.jpeg,.png,.docx");
    expect(input).toHaveAttribute("aria-describedby", "vault-upload-formats");
    expect(
      screen.getByText("PDF, JPG, PNG, or DOCX up to 4 MB"),
    ).toBeInTheDocument();
  });

  it("forwards the typed purpose to upload()", () => {
    render(<VaultUploadZone practiceId={42} />);
    const purposeInput = screen.getByLabelText(
      /what is this document for/i,
    ) as HTMLInputElement;
    fireEvent.change(purposeInput, {
      target: { value: "Passport for KITAS renewal" },
    });
    const input = screen.getByLabelText(
      "Choose file to upload",
    ) as HTMLInputElement;
    const file = new File(["x"], "a.pdf", { type: "application/pdf" });
    Object.defineProperty(input, "files", { value: [file] });
    fireEvent.change(input);
    expect(mockUpload.mock.calls[0][1]).toEqual({
      practiceId: 42,
      purpose: "Passport for KITAS renewal",
      documentType: "other",
    });
  });

  it("renders error state when upload state is error", () => {
    mockState = { status: "error", message: "Upload failed (413)" };
    render(<VaultUploadZone />);
    expect(screen.getByRole("alert")).toHaveTextContent(/Upload failed/i);
    expect(
      screen.queryByRole("button", { name: "Retry upload" }),
    ).not.toBeInTheDocument();
  });

  it("retries the retained upload from a retryable error", () => {
    mockState = { status: "error", message: "Network error" };
    mockCanRetry = true;
    render(<VaultUploadZone />);

    fireEvent.click(screen.getByRole("button", { name: "Retry upload" }));

    expect(mockRetry).toHaveBeenCalledTimes(1);
  });

  it("renders done state with uploaded filename", () => {
    mockState = {
      status: "done",
      file: { name: "ok.pdf" },
    };
    render(<VaultUploadZone />);
    expect(screen.getByRole("status")).toHaveTextContent(/Uploaded: ok\.pdf/i);
  });
});
